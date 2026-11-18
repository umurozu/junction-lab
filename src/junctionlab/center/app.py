"""REST + WebSocket API of the central management system."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from .. import sdnotify
from ..ntcip import mib as ntcip
from ..proto.frame import Command
from ..snmp.client import SnmpError, SnmpTimeout
from .auth import TokenService, current_user_dep, hash_password, require_operator, verify_password
from .db import Count, Database, Event, User
from .service import FieldService, event_dict
from .settings import Settings

log = logging.getLogger("center.api")


class LoginIn(BaseModel):
    username: str
    password: str


class PatternIn(BaseModel):
    pattern: int = Field(ge=1, le=255)


class PreemptIn(BaseModel):
    stage: int = Field(ge=0, le=15)


class FaultIn(BaseModel):
    code: int = Field(ge=1, le=4, description="1 conflict, 2 red lamp out, 3 detector stuck, 4 power fail")
    phase: int = Field(ge=1, le=16)


class PhaseTimingIn(BaseModel):
    min_green: int | None = Field(None, ge=1, le=255)
    max_green: int | None = Field(None, ge=1, le=255)


async def seed_users(db: Database, s: Settings) -> None:
    async with db.session() as session:
        for name, pw, role in ((s.admin_user, s.admin_password, "operator"),
                               (s.viewer_user, s.viewer_password, "viewer")):
            exists = (await session.execute(select(User).where(User.username == name))).scalar_one_or_none()
            if not exists:
                session.add(User(username=name, password_hash=hash_password(pw), role=role))
        await session.commit()


def create_app(settings: Settings | None = None, start_field: bool = True) -> FastAPI:
    settings = settings or Settings()
    db = Database(settings.database_url)
    field = FieldService(settings, db)
    tokens = TokenService(settings.jwt_secret, settings.jwt_hours)
    current_user = current_user_dep(tokens)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.create_all()
        await seed_users(db, settings)
        if start_field:
            await field.start()
        else:
            await field.load_junctions()
        sdnotify.notify("READY=1")  # systemd Type=notify
        yield
        await field.stop()
        await db.close()

    app = FastAPI(title="junction-lab center", version="0.1.0", lifespan=lifespan)
    app.state.field, app.state.db, app.state.tokens = field, db, tokens
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"],
                       allow_headers=["*"])

    def junction_or_404(jid: int):
        j = field.junctions.get(jid)
        if j is None:
            raise HTTPException(404, "junction not found")
        return j

    async def audit(jid: int, user: dict, action: str, ok: bool, detail: str = "") -> None:
        await field.record_event(jid, "command", "info" if ok else "warning", "api",
                                 f"{user['sub']}: {action}" + ("" if ok else f" FAILED ({detail})"),
                                 {"user": user["sub"], "action": action, "ok": ok})

    # ------------------------------------------------------------------ auth
    @app.post("/api/auth/login")
    async def login(body: LoginIn):
        async with db.session() as s:
            user = (await s.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
        if user is None or not verify_password(body.password, user.password_hash):
            raise HTTPException(401, "invalid username or password")
        return {"access_token": tokens.issue(user.username, user.role), "token_type": "bearer",
                "role": user.role, "username": user.username}

    @app.get("/api/auth/me")
    async def me(user: dict = Depends(current_user)):
        return {"username": user["sub"], "role": user["role"]}

    # ------------------------------------------------------------------ read
    @app.get("/api/health")
    async def health():
        online = sum(j.online for j in field.junctions.values())
        return {"status": "ok", "junctions": len(field.junctions), "online": online}

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics():
        return field.prometheus()

    @app.get("/api/junctions")
    async def list_junctions(user: dict = Depends(current_user)):
        return [j.public() for j in field.junctions.values()]

    @app.get("/api/junctions/{jid}")
    async def get_junction(jid: int, user: dict = Depends(current_user)):
        j = junction_or_404(jid)
        return {**j.public(), **j.meta(), "history": list(j.history)}

    @app.get("/api/junctions/{jid}/timing")
    async def get_timing(jid: int, user: dict = Depends(current_user)):
        """Live NTCIP phaseTable read via SNMP GETBULK walk."""
        junction_or_404(jid)
        client = field.clients[jid]
        rows: dict[int, dict] = {}
        cols = {ntcip.PH_MIN_GREEN: "min_green", ntcip.PH_MAX1: "max_green", ntcip.PH_PASSAGE: "passage_ds",
                ntcip.PH_WALK: "walk", ntcip.PH_PED_CLEAR: "ped_clear", ntcip.PH_YELLOW: "yellow_ds",
                ntcip.PH_RED_CLEAR: "red_clear_ds"}
        try:
            async for oid, val in client.walk(ntcip.PHASE_TABLE):
                col, phase = oid[-2], oid[-1]
                if col in cols:
                    rows.setdefault(phase, {"phase": phase})[cols[col]] = int(val)
        except SnmpTimeout as exc:
            raise HTTPException(504, "device not responding") from exc
        return sorted(rows.values(), key=lambda r: r["phase"])

    @app.get("/api/events")
    async def events(junction_id: int | None = None, severity: str | None = None,
                     limit: int = Query(100, le=1000), user: dict = Depends(current_user)):
        q = select(Event).order_by(desc(Event.ts)).limit(limit)
        if junction_id is not None:
            q = q.where(Event.junction_id == junction_id)
        if severity:
            q = q.where(Event.severity == severity)
        async with db.session() as s:
            return [event_dict(e) for e in (await s.execute(q)).scalars()]

    @app.get("/api/junctions/{jid}/counts")
    async def counts(jid: int, minutes: int = Query(60, le=24 * 60), bucket: int = Query(5, ge=1, le=60),
                     user: dict = Depends(current_user)):
        """Detector volumes aggregated into `bucket`-minute bins, as veh/h per phase."""
        junction_or_404(jid)
        since = datetime.now(UTC) - timedelta(minutes=minutes)
        async with db.session() as s:
            rows = (await s.execute(select(Count).where(Count.junction_id == jid, Count.ts >= since)
                                    .order_by(Count.ts))).scalars().all()
        bins: dict[str, dict[int, int]] = {}
        for r in rows:
            ts = r.ts if r.ts.tzinfo else r.ts.replace(tzinfo=UTC)
            b = ts.replace(second=0, microsecond=0, minute=ts.minute - ts.minute % bucket).isoformat()
            bins.setdefault(b, {}).setdefault(r.phase, 0)
            bins[b][r.phase] += r.volume
        scale = 60 / bucket
        return [{"t": t, "veh_per_hour": {p: round(v * scale) for p, v in ph.items()}} for t, ph in bins.items()]

    # ------------------------------------------------------------------ control
    @app.post("/api/junctions/{jid}/pattern")
    async def set_pattern(jid: int, body: PatternIn, user: dict = Depends(current_user)):
        require_operator(user)
        j = junction_or_404(jid)
        name = next((p["name"] for p in j.meta()["patterns"] if p["number"] == body.pattern), str(body.pattern))
        try:  # NTCIP path: SNMP SET systemPatternControl
            await field.clients[jid].set((ntcip.SYSTEM_PATTERN_CONTROL, body.pattern))
        except SnmpError as exc:
            await audit(jid, user, f"plan → {name}", False, str(exc))
            raise HTTPException(400, f"device rejected: {exc}") from exc
        except SnmpTimeout as exc:
            await audit(jid, user, f"plan → {name}", False, "timeout")
            raise HTTPException(504, "device not responding") from exc
        await audit(jid, user, f"plan → {name}", True)
        return {"ok": True, "pattern": body.pattern}

    @app.patch("/api/junctions/{jid}/phases/{phase}")
    async def set_phase_timing(jid: int, phase: int, body: PhaseTimingIn, user: dict = Depends(current_user)):
        require_operator(user)
        junction_or_404(jid)
        pairs = []
        if body.min_green is not None:
            pairs.append((ntcip.phase_oid(ntcip.PH_MIN_GREEN, phase), body.min_green))
        if body.max_green is not None:
            pairs.append((ntcip.phase_oid(ntcip.PH_MAX1, phase), body.max_green))
        if not pairs:
            raise HTTPException(422, "nothing to change")
        try:
            await field.clients[jid].set(*pairs)
        except (SnmpError, SnmpTimeout) as exc:
            raise HTTPException(400, str(exc)) from exc
        await audit(jid, user, f"phase {phase} timings {body.model_dump(exclude_none=True)}", True)
        return {"ok": True}

    async def jlp_command(jid: int, user: dict, cmd: Command, label: str, arg: int = 0, arg2: int = 0):
        require_operator(user)
        junction_or_404(jid)
        ok, reason = await field.send_command(jid, cmd, arg, arg2)
        await audit(jid, user, label, ok, reason)
        if not ok:
            raise HTTPException(502 if "timeout" in reason.lower() else 400, reason)
        return {"ok": True}

    @app.post("/api/junctions/{jid}/preempt")
    async def preempt(jid: int, body: PreemptIn, user: dict = Depends(current_user)):
        return await jlp_command(jid, user, Command.PREEMPT, f"emergency preemption → stage {body.stage + 1}",
                                 body.stage)

    @app.post("/api/junctions/{jid}/faults")
    async def inject_fault(jid: int, body: FaultIn, user: dict = Depends(current_user)):
        names = {1: "conflict", 2: "red lamp failure", 3: "stuck detector", 4: "mains power failure"}
        return await jlp_command(jid, user, Command.INJECT_FAULT,
                                 f"fault simulation: {names[body.code]} (phase {body.phase})", body.code, body.phase)

    @app.delete("/api/junctions/{jid}/faults")
    async def clear_faults(jid: int, user: dict = Depends(current_user)):
        return await jlp_command(jid, user, Command.CLEAR_FAULTS, "clear faults")

    @app.post("/api/junctions/{jid}/ped/{phase}")
    async def ped(jid: int, phase: int, user: dict = Depends(current_user)):
        return await jlp_command(jid, user, Command.PED_REQUEST, f"pedestrian button phase {phase}", phase)

    # ------------------------------------------------------------------ live
    @app.websocket("/ws/live")
    async def live(ws: WebSocket, token: str = ""):
        try:
            tokens.decode(token)
        except HTTPException:
            await ws.close(code=4401)
            return
        await ws.accept()
        q = field.subscribe()
        try:
            await ws.send_json({"type": "snapshot", "junctions": [j.public() for j in field.junctions.values()]})
            while True:
                msg = await q.get()
                await ws.send_json(msg)
        except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
            pass
        finally:
            field.unsubscribe(q)

    # ------------------------------------------------------------------ SPA
    static = Path(settings.static_dir)
    if static.is_dir():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            f = static / path
            return FileResponse(f if path and f.is_file() else static / "index.html")

    return app
