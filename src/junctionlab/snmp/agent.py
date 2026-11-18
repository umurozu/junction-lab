"""Asyncio SNMPv2c agent with a pluggable MIB tree.

Each managed object is registered with a getter and, optionally, a setter.
GET / GETNEXT / GETBULK / SET are supported, plus sending v2c traps.
"""

from __future__ import annotations

import asyncio
import bisect
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..netutil import udp_endpoint
from . import ber, pdu
from .ber import Oid, TimeTicks
from .pdu import Message

log = logging.getLogger("snmp.agent")

Getter = Callable[[], Any]
Setter = Callable[[Any], None]


class SetError(Exception):
    """Raise from a setter to return a specific SNMP error-status."""

    def __init__(self, status: int, reason: str = "") -> None:
        super().__init__(reason or pdu.ERROR_NAMES.get(status, str(status)))
        self.status = status


@dataclass
class Managed:
    get: Getter
    set: Setter | None = None
    value_type: type | tuple[type, ...] | None = int


class MibTree:
    def __init__(self) -> None:
        self._objs: dict[Oid, Managed] = {}
        self._keys: list[Oid] = []

    def register(self, oid: Oid | str, get: Getter, set: Setter | None = None,
                 value_type: type | tuple[type, ...] | None = int) -> None:
        oid = Oid(oid)
        if oid not in self._objs:
            bisect.insort(self._keys, oid)
        self._objs[oid] = Managed(get, set, value_type)

    def scalar(self, oid: Oid | str, value: Any) -> None:
        self.register(oid, lambda v=value: v, value_type=None)

    def get(self, oid: Oid) -> Managed | None:
        return self._objs.get(oid)

    def next(self, oid: Oid) -> Oid | None:
        i = bisect.bisect_right(self._keys, oid)
        return self._keys[i] if i < len(self._keys) else None

    def __len__(self) -> int:
        return len(self._keys)


@dataclass
class AgentStats:
    in_pkts: int = 0
    out_pkts: int = 0
    bad_community: int = 0
    parse_errors: int = 0
    sets: int = 0
    traps: int = 0


class SnmpAgent(asyncio.DatagramProtocol):
    def __init__(self, mib: MibTree, read_community: bytes = b"public",
                 write_community: bytes = b"private") -> None:
        self.mib = mib
        self.read_community = read_community
        self.write_community = write_community
        self.stats = AgentStats()
        self.started = time.monotonic()
        self.transport: asyncio.DatagramTransport | None = None
        self.trap_targets: list[tuple[str, int]] = []
        self._trap_id = 0

    # ------------------------------------------------------------ lifecycle
    async def start(self, host: str = "0.0.0.0", port: int = 1161) -> None:
        await udp_endpoint(lambda: self, host, port)
        log.info("SNMP agent listening on udp/%s:%d (%d objects)", host, port, len(self.mib))

    def connection_made(self, transport) -> None:  # type: ignore[override]
        self.transport = transport

    def uptime(self) -> TimeTicks:
        return TimeTicks(int((time.monotonic() - self.started) * 100) & 0xFFFFFFFF)

    # ------------------------------------------------------------ requests
    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        self.stats.in_pkts += 1
        try:
            req = Message.decode(data)
        except ber.BERError as exc:
            self.stats.parse_errors += 1
            log.warning("malformed SNMP packet from %s: %s", addr, exc)
            return
        resp = self.handle(req)
        if resp is not None and self.transport is not None:
            self.transport.sendto(resp.encode(), addr)
            self.stats.out_pkts += 1

    def handle(self, req: Message) -> Message | None:
        is_set = req.pdu_type == pdu.SET
        allowed = (self.write_community,) if is_set else (self.read_community, self.write_community)
        if req.community not in allowed:
            # RFC 3584: silently drop + count (snmpInBadCommunityNames)
            self.stats.bad_community += 1
            return None
        handler = {pdu.GET: self._get, pdu.GET_NEXT: self._get_next, pdu.GET_BULK: self._get_bulk,
                   pdu.SET: self._set}.get(req.pdu_type)
        if handler is None:
            return None
        status, index, varbinds = handler(req)
        return Message(pdu.RESPONSE, req.request_id, varbinds, req.community, req.version, status, index)

    def _read(self, oid: Oid) -> Any:
        obj = self.mib.get(oid)
        if obj is None:
            return ber.NoSuchObject
        try:
            return obj.get()
        except Exception:  # never crash the agent on a bad getter
            log.exception("getter for %s failed", oid)
            return ber.NoSuchInstance

    def _get(self, req: Message):
        return 0, 0, [(oid, self._read(oid)) for oid, _ in req.varbinds]

    def _get_next(self, req: Message):
        out = []
        for oid, _ in req.varbinds:
            nxt = self.mib.next(oid)
            out.append((oid, ber.EndOfMibView) if nxt is None else (nxt, self._read(nxt)))
        return 0, 0, out

    def _get_bulk(self, req: Message):
        non_rep, max_rep = max(req.error_status, 0), max(req.error_index, 0)
        out = []
        for oid, _ in req.varbinds[:non_rep]:
            nxt = self.mib.next(oid)
            out.append((oid, ber.EndOfMibView) if nxt is None else (nxt, self._read(nxt)))
        cursors = [oid for oid, _ in req.varbinds[non_rep:]]
        for _ in range(min(max_rep, 64)):
            if not cursors:
                break
            for i, cur in enumerate(cursors):
                nxt = self.mib.next(cur)
                if nxt is None:
                    out.append((cur, ber.EndOfMibView))
                else:
                    out.append((nxt, self._read(nxt)))
                    cursors[i] = nxt
        return 0, 0, out

    def _set(self, req: Message):
        # phase 1: validate every varbind (RFC 3416 "as if simultaneously")
        for i, (oid, value) in enumerate(req.varbinds, start=1):
            obj = self.mib.get(oid)
            if obj is None:
                return pdu.NO_ACCESS, i, req.varbinds
            if obj.set is None:
                return pdu.NOT_WRITABLE, i, req.varbinds
            if obj.value_type is not None and not isinstance(value, obj.value_type):
                return pdu.WRONG_TYPE, i, req.varbinds
        # phase 2: commit
        for i, (oid, value) in enumerate(req.varbinds, start=1):
            try:
                self.mib.get(oid).set(value)  # type: ignore[union-attr]
            except SetError as exc:
                return exc.status, i, req.varbinds
            except Exception:
                log.exception("setter for %s failed", oid)
                return pdu.GEN_ERR, i, req.varbinds
        self.stats.sets += 1
        log.info("SET %s", ", ".join(f"{o}={v!r}" for o, v in req.varbinds))
        return 0, 0, [(oid, self._read(oid)) for oid, _ in req.varbinds]

    # ------------------------------------------------------------ traps
    def send_trap(self, trap_oid: Oid, varbinds: list[tuple[Oid, Any]] | None = None) -> None:
        if not self.transport or not self.trap_targets:
            return
        self._trap_id = (self._trap_id + 1) & 0x7FFFFFFF
        msg = Message(pdu.TRAP_V2, self._trap_id,
                      [(pdu.SYS_UPTIME_0, self.uptime()), (pdu.SNMP_TRAP_OID_0, trap_oid), *(varbinds or [])],
                      self.read_community)
        raw = msg.encode()
        for target in self.trap_targets:
            self.transport.sendto(raw, target)
        self.stats.traps += 1
