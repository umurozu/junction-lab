"""Field communication layer of the center.

* polls every junction over SNMP (NTCIP 1202 phase status) once per second
* receives SNMPv2c traps (alarms)
* receives JLP frames: STATUS heartbeat, COUNTS volumes, ALARM
* sends JLP COMMANDs and waits for the matching ACK
* keeps the live state in memory and fans it out to WebSocket clients
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import socket
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from ..controller.config import PRESETS
from ..controller.fsm import Alarm, Interval, Mode
from ..netutil import udp_endpoint
from ..ntcip import mib as ntcip
from ..proto import frame as jlp
from ..proto.frame import Command, Frame, MsgType
from ..snmp.client import SnmpClient, SnmpError, SnmpTimeout, TrapReceiver
from .db import Count, Database, Event, Junction
from .settings import Settings

log = logging.getLogger("center")

POLL_OIDS = {
    "greens": ntcip.status_oid(ntcip.PSG_GREENS),
    "yellows": ntcip.status_oid(ntcip.PSG_YELLOWS),
    "reds": ntcip.status_oid(ntcip.PSG_REDS),
    "walks": ntcip.status_oid(ntcip.PSG_WALKS),
    "ped_clears": ntcip.status_oid(ntcip.PSG_PED_CLEARS),
    "veh_calls": ntcip.status_oid(ntcip.PSG_VEH_CALLS),
    "pattern": ntcip.COORD_PATTERN_STATUS,
    "red_yellows": ntcip.JL_RED_YELLOWS,
    "mode": ntcip.JL_CONTROL_MODE,
    "stage": ntcip.JL_CURRENT_STAGE,
    "interval": ntcip.JL_INTERVAL,
    "alarms": ntcip.JL_ALARMS,
    "flashing": ntcip.JL_FLASHING,
    "cycle": ntcip.JL_CYCLE_COUNT,
}
TRAFFIC_OIDS = {  # per-phase columns -> state[name] = {phase: value}
    "queues": ntcip.JL_QUEUE,
    "served": ntcip.JL_SERVED,
    "peds_waiting": ntcip.JL_PED_WAITING,
    "peds_crossed": ntcip.JL_PED_CROSSED,
}
BITMASKS = {"greens", "yellows", "reds", "walks", "ped_clears", "veh_calls", "red_yellows"}
OFFLINE_AFTER = 3  # consecutive poll failures


@dataclass
class Metrics:
    polls: int = 0
    poll_timeouts: int = 0
    poll_latency_sum: float = 0.0
    traps: int = 0
    jlp_frames: collections.Counter = field(default_factory=collections.Counter)
    jlp_bad_frames: int = 0
    commands: int = 0
    command_failures: int = 0
    ws_clients: int = 0


@dataclass
class LiveJunction:
    id: int
    name: str
    preset: str
    lat: float
    lon: float
    host: str
    snmp_port: int
    jlp_port: int
    online: bool = False
    failures: int = 0
    last_seen: float = 0.0
    last_heartbeat: float = 0.0
    latency_ms: float = 0.0
    state: dict[str, Any] = field(default_factory=dict)
    volume_total: dict[int, int] = field(default_factory=dict)
    history: collections.deque = field(default_factory=lambda: collections.deque(maxlen=180))

    def meta(self) -> dict:
        cfg = PRESETS[self.preset]()
        return {
            "phases": [{"number": p.number, "name": p.name, "approach": p.approach, "has_ped": p.has_ped,
                        "min_green": p.min_green, "max_green": p.max_green} for p in cfg.phases.values()],
            "stages": [{"name": s.name, "phases": list(s.phases)} for s in cfg.stages],
            "patterns": [{"number": n, "name": cfg.pattern_names.get(n, f"Plan {n}"), "greens": g}
                         for n, g in sorted(cfg.patterns.items())]
                        + [{"number": n, "name": cfg.pattern_names[n], "greens": None} for n in (254, 255)],
        }

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "preset": self.preset, "lat": self.lat, "lon": self.lon,
                "online": self.online, "latency_ms": round(self.latency_ms, 1),
                "last_seen": self.last_seen and datetime.fromtimestamp(self.last_seen, UTC).isoformat(),
                "heartbeat_age_s": round(time.time() - self.last_heartbeat, 1) if self.last_heartbeat else None,
                "state": self.state, "volume_total": self.volume_total}


class _JlpServer(asyncio.DatagramProtocol):
    def __init__(self, center: FieldService) -> None:
        self.center = center
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        self.center.on_jlp(data, addr)


class FieldService:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings, self.db = settings, db
        self.junctions: dict[int, LiveJunction] = {}
        self.clients: dict[int, SnmpClient] = {}
        self.metrics = Metrics()
        self.subscribers: set[asyncio.Queue] = set()
        self.tasks: list[asyncio.Task] = []
        self.jlp: _JlpServer | None = None
        self.trap_rx: TrapReceiver | None = None
        self._acks: dict[tuple[int, int], asyncio.Future] = {}
        self._seq = 0
        self._last_counts: dict[int, dict[int, int]] = {}

    # ---------------------------------------------------------------- setup
    async def load_junctions(self) -> None:
        async with self.db.session() as s:
            rows = (await s.execute(select(Junction))).scalars().all()
            if not rows:
                seed = await asyncio.to_thread(_read_json, self.settings.junctions_file)
                for j in seed:
                    s.add(Junction(id=j["device_id"], name=j["name"], preset=j["preset"], lat=j["lat"],
                                   lon=j["lon"], host=j["host"], snmp_port=j["snmp_port"], jlp_port=j["jlp_port"]))
                await s.commit()
                rows = (await s.execute(select(Junction))).scalars().all()
        for r in rows:
            self.junctions[r.id] = LiveJunction(r.id, r.name, r.preset, r.lat, r.lon, r.host, r.snmp_port,
                                                r.jlp_port)
            self.clients[r.id] = SnmpClient(r.host, r.snmp_port, self.settings.snmp_community,
                                            self.settings.snmp_write_community, timeout=0.8, retries=1)
        log.info("loaded %d junctions", len(rows))

    async def start(self) -> None:
        await self.load_junctions()
        _, self.jlp = await udp_endpoint(lambda: _JlpServer(self), "0.0.0.0", self.settings.jlp_port)
        self.trap_rx = await TrapReceiver.listen(self.on_trap, "0.0.0.0", self.settings.trap_port,
                                                 ) if self.settings.trap_port else None
        log.info("JLP on udp/%d, traps on udp/%d", self.settings.jlp_port, self.settings.trap_port)
        for jid in self.junctions:
            self.tasks.append(asyncio.create_task(self._poll_loop(jid), name=f"poll-{jid}"))

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for c in self.clients.values():
            c.close()
        if self.jlp and self.jlp.transport:
            self.jlp.transport.close()
        if self.trap_rx and self.trap_rx.transport:
            self.trap_rx.transport.close()

    # ---------------------------------------------------------------- pub/sub
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.subscribers.add(q)
        self.metrics.ws_clients = len(self.subscribers)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)
        self.metrics.ws_clients = len(self.subscribers)

    def publish(self, msg: dict) -> None:
        for q in list(self.subscribers):
            if q.full():  # slow client: drop oldest, never block the poller
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(msg)

    async def record_event(self, jid: int, kind: str, severity: str, source: str, message: str,
                           data: dict | None = None) -> None:
        ev = Event(junction_id=jid, kind=kind, severity=severity, source=source, message=message,
                   data=data or {})
        async with self.db.session() as s:
            s.add(ev)
            await s.commit()
        log.log(logging.WARNING if severity != "info" else logging.INFO, "[j%d] %s: %s", jid, kind, message)
        self.publish({"type": "event", "event": event_dict(ev)})

    # ---------------------------------------------------------------- polling
    async def _poll_loop(self, jid: int) -> None:
        j, client = self.junctions[jid], self.clients[jid]
        names, oids = list(POLL_OIDS), list(POLL_OIDS.values())
        phase_nums = sorted(PRESETS[j.preset]().phases)
        traffic = [(name, p, col + (p,)) for name, col in TRAFFIC_OIDS.items() for p in phase_nums]
        oids += [o for _, _, o in traffic]
        while True:
            t0 = time.perf_counter()
            try:
                vals = await client.get(*oids)
                lat = time.perf_counter() - t0
                self.metrics.polls += 1
                self.metrics.poll_latency_sum += lat
                state = {}
                for name, oid in zip(names, oids):
                    v = vals.get(oid)
                    v = int(v) if isinstance(v, int) else 0
                    state[name] = ntcip.bits(v) if name in BITMASKS else v
                for name, p, oid in traffic:
                    v = vals.get(oid)
                    state.setdefault(name, {})[str(p)] = int(v) if isinstance(v, int) else 0
                await self._apply_state(j, state, lat)
            except (SnmpTimeout, SnmpError, OSError) as exc:
                self.metrics.poll_timeouts += 1
                j.failures += 1
                if j.online and j.failures >= OFFLINE_AFTER:
                    j.online = False
                    await self.record_event(jid, "comm", "warning", "snmp-poll",
                                            f"Communication lost ({type(exc).__name__})")
                    self.publish({"type": "junction", "junction": j.public()})
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("poll loop for junction %d failed", jid)
            await asyncio.sleep(max(0.0, self.settings.poll_interval - (time.perf_counter() - t0)))

    async def _apply_state(self, j: LiveJunction, state: dict, latency: float) -> None:
        prev = j.state
        state["mode_name"] = Mode(state["mode"]).name if state["mode"] in Mode._value2member_map_ else "?"
        state["interval_name"] = (Interval(state["interval"]).name
                                  if state["interval"] in Interval._value2member_map_ else "?")
        state["alarm_names"] = [a.name for a in Alarm if a and a & state["alarms"]]  # type: ignore[operator]
        state["flashing"] = bool(state["flashing"])
        j.state, j.latency_ms, j.failures, j.last_seen = state, latency * 1000, 0, time.time()
        j.history.append({"t": round(j.last_seen, 1), "g": state["greens"], "y": state["yellows"],
                          "ry": state["red_yellows"], "f": state["flashing"]})
        if not j.online:
            j.online = True
            await self.record_event(j.id, "comm", "info", "snmp-poll", "Device online")
        if prev and prev.get("mode") != state["mode"]:
            await self.record_event(j.id, "mode", "warning" if state["mode"] == Mode.FLASH else "info",
                                    "snmp-poll", f"Operating mode: {prev.get('mode_name')} → {state['mode_name']}",
                                    {"old": prev.get("mode"), "new": state["mode"]})
        self.publish({"type": "state", "id": j.id, "online": True, "latency_ms": round(j.latency_ms, 1),
                      "state": state, "t": j.last_seen})

    # ---------------------------------------------------------------- traps
    def on_trap(self, addr, trap_oid, varbinds) -> None:
        self.metrics.traps += 1
        j = self._junction_by_host(addr)
        if j is None:
            log.warning("trap from unknown device %s: %s", addr, trap_oid)
            return
        vb = {str(o): v for o, v in varbinds}
        if trap_oid == ntcip.TRAP_ALARM:
            flags = Alarm(int(vb.get(str(ntcip.JL_ALARMS), 0)))
            phase = int(vb.get(str(ntcip.JL + (12, 0)), 0))
            critical = bool(flags & (Alarm.CONFLICT | Alarm.RED_LAMP_OUT))
            names = ", ".join(a.name for a in Alarm if a and a in flags)  # type: ignore[misc]
            asyncio.create_task(self.record_event(
                j.id, "alarm", "critical" if critical else "warning", "snmp-trap",
                f"Alarm: {names} (phase {phase})", {"alarms": int(flags), "phase": phase}))
        elif trap_oid == ntcip.TRAP_ALARM_CLEARED:
            asyncio.create_task(self.record_event(j.id, "alarm_cleared", "info", "snmp-trap",
                                                  "Alarms cleared"))

    def _ip(self, host: str) -> str:
        """Junctions may be configured by hostname (DNS / docker / VPN); traps arrive from an IP."""
        try:
            return socket.gethostbyname(host)
        except OSError:
            return host

    def _junction_by_host(self, addr) -> LiveJunction | None:
        host, port = addr[0], addr[1]
        exact = [j for j in self.junctions.values() if self._ip(j.host) == host and j.snmp_port == port]
        if exact:
            return exact[0]
        same_host = [j for j in self.junctions.values() if self._ip(j.host) == host]
        return same_host[0] if len(same_host) == 1 else None

    # ---------------------------------------------------------------- JLP
    def on_jlp(self, data: bytes, addr) -> None:
        try:
            f = Frame.decode(data)
        except jlp.FrameError as exc:
            self.metrics.jlp_bad_frames += 1
            log.warning("bad JLP frame from %s: %s", addr, exc)
            return
        self.metrics.jlp_frames[f.type.name] += 1
        j = self.junctions.get(f.device)
        if j is None:
            log.warning("JLP frame from unknown device %d (%s)", f.device, addr)
            return
        if f.type == MsgType.STATUS:
            j.last_heartbeat = time.time()
        elif f.type == MsgType.COUNTS:
            asyncio.create_task(self._store_counts(j, jlp.unpack_counts(f.payload)))
        elif f.type == MsgType.ACK:
            seq, ok, reason = jlp.unpack_ack(f.payload)
            fut = self._acks.pop((f.device, seq), None)
            if fut and not fut.done():
                fut.set_result((ok, reason))

    async def _store_counts(self, j: LiveJunction, totals: dict[int, int]) -> None:
        last = self._last_counts.get(j.id)
        self._last_counts[j.id] = totals
        j.volume_total = totals
        if last is None:
            return  # first report after (re)start: baseline only
        now = datetime.now(UTC)
        rows = []
        for phase, total in totals.items():
            delta = total - last.get(phase, 0)
            if delta < 0:  # device restarted, counter reset
                delta = total
            rows.append(Count(ts=now, junction_id=j.id, phase=phase, volume=delta))
        async with self.db.session() as s:
            s.add_all(rows)
            await s.commit()
        self.publish({"type": "counts", "id": j.id, "t": now.isoformat(),
                      "volumes": {r.phase: r.volume for r in rows}, "totals": totals})

    async def send_command(self, jid: int, cmd: Command, arg: int = 0, arg2: int = 0,
                           timeout: float = 1.5) -> tuple[bool, str]:
        j = self.junctions[jid]
        if not self.jlp or not self.jlp.transport:
            return False, "JLP endpoint not ready"
        self._seq = (self._seq + 1) & 0xFFFF
        seq = self._seq
        fut = asyncio.get_running_loop().create_future()
        self._acks[(jid, seq)] = fut
        frame = Frame(MsgType.COMMAND, jid, jlp.pack_command(cmd, arg, arg2), seq).encode()
        self.metrics.commands += 1
        for _ in range(2):  # one retry
            self.jlp.transport.sendto(frame, (j.host, j.jlp_port))
            try:
                return await asyncio.wait_for(asyncio.shield(fut), timeout / 2)
            except TimeoutError:
                continue
        self._acks.pop((jid, seq), None)
        self.metrics.command_failures += 1
        return False, "ACK timeout"

    # ---------------------------------------------------------------- metrics
    def prometheus(self) -> str:
        m = self.metrics
        lines = [
            "# TYPE jl_snmp_polls_total counter", f"jl_snmp_polls_total {m.polls}",
            "# TYPE jl_snmp_poll_failures_total counter", f"jl_snmp_poll_failures_total {m.poll_timeouts}",
            "# TYPE jl_snmp_poll_latency_seconds_sum counter",
            f"jl_snmp_poll_latency_seconds_sum {m.poll_latency_sum:.6f}",
            "# TYPE jl_snmp_traps_total counter", f"jl_snmp_traps_total {m.traps}",
            "# TYPE jl_jlp_bad_frames_total counter", f"jl_jlp_bad_frames_total {m.jlp_bad_frames}",
            "# TYPE jl_commands_total counter", f"jl_commands_total {m.commands}",
            "# TYPE jl_command_failures_total counter", f"jl_command_failures_total {m.command_failures}",
            "# TYPE jl_ws_clients gauge", f"jl_ws_clients {m.ws_clients}",
            "# TYPE jl_jlp_frames_total counter",
            *(f'jl_jlp_frames_total{{type="{t}"}} {n}' for t, n in sorted(m.jlp_frames.items())),
            "# TYPE jl_junction_online gauge",
            *(f'jl_junction_online{{junction="{j.id}"}} {int(j.online)}' for j in self.junctions.values()),
            "# TYPE jl_junction_poll_latency_ms gauge",
            *(f'jl_junction_poll_latency_ms{{junction="{j.id}"}} {j.latency_ms:.2f}'
              for j in self.junctions.values()),
        ]
        return "\n".join(lines) + "\n"


def event_dict(e: Event) -> dict:
    return {"id": e.id, "ts": e.ts.isoformat() if e.ts else None, "junction_id": e.junction_id, "kind": e.kind,
            "severity": e.severity, "source": e.source, "message": e.message, "data": e.data}


def _read_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
