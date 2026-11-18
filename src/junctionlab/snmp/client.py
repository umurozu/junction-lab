"""Asyncio SNMPv2c manager (client) and trap receiver."""

from __future__ import annotations

import asyncio
import itertools
import logging
import random
from collections.abc import AsyncIterator, Callable
from typing import Any

from ..netutil import udp_endpoint
from . import ber, pdu
from .ber import Oid
from .pdu import Message

log = logging.getLogger("snmp.client")


class SnmpTimeout(TimeoutError):
    pass


class SnmpError(Exception):
    def __init__(self, status: int, index: int) -> None:
        super().__init__(f"{pdu.ERROR_NAMES.get(status, status)} at varbind {index}")
        self.status, self.index = status, index


class _ClientProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.pending: dict[int, asyncio.Future] = {}

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        try:
            msg = Message.decode(data)
        except ber.BERError as exc:
            log.warning("bad response from %s: %s", addr, exc)
            return
        fut = self.pending.pop(msg.request_id, None)
        if fut and not fut.done():
            fut.set_result(msg)

    def error_received(self, exc) -> None:  # type: ignore[override]
        # e.g. ICMP port unreachable on Windows/Linux - let the timeout handle it
        log.debug("socket error: %s", exc)


class SnmpClient:
    """One client per target device. Reuses a single UDP socket."""

    def __init__(self, host: str, port: int = 161, community: str = "public",
                 write_community: str = "private", timeout: float = 1.0, retries: int = 2) -> None:
        self.host, self.port = host, port
        self.community = community.encode()
        self.write_community = write_community.encode()
        self.timeout, self.retries = timeout, retries
        self._ids = itertools.count(random.randint(1, 1 << 20))
        self._transport: asyncio.DatagramTransport | None = None
        self._proto: _ClientProtocol | None = None

    async def _ensure(self) -> None:
        if self._transport is None:
            self._transport, self._proto = await udp_endpoint(_ClientProtocol, "0.0.0.0", 0,
                                                              remote=(self.host, self.port))

    def close(self) -> None:
        if self._transport:
            self._transport.close()
            self._transport = None

    async def request(self, pdu_type: int, varbinds: list[tuple[Oid, Any]], community: bytes | None = None,
                      error_status: int = 0, error_index: int = 0) -> Message:
        await self._ensure()
        assert self._proto and self._transport
        for attempt in range(self.retries + 1):
            rid = next(self._ids) & 0x7FFFFFFF
            msg = Message(pdu_type, rid, varbinds, community or self.community,
                          error_status=error_status, error_index=error_index)
            fut = asyncio.get_running_loop().create_future()
            self._proto.pending[rid] = fut
            self._transport.sendto(msg.encode())
            try:
                resp: Message = await asyncio.wait_for(fut, self.timeout)
            except TimeoutError:
                self._proto.pending.pop(rid, None)
                log.debug("timeout %s:%d attempt %d", self.host, self.port, attempt + 1)
                continue
            if resp.error_status:
                raise SnmpError(resp.error_status, resp.error_index)
            return resp
        raise SnmpTimeout(f"no response from {self.host}:{self.port}")

    async def get(self, *oids: Oid | str) -> dict[Oid, Any]:
        resp = await self.request(pdu.GET, [(Oid(o), None) for o in oids])
        return dict(resp.varbinds)

    async def get_one(self, oid: Oid | str) -> Any:
        return (await self.get(oid))[Oid(oid)]

    async def set(self, *pairs: tuple[Oid | str, Any]) -> dict[Oid, Any]:
        resp = await self.request(pdu.SET, [(Oid(o), v) for o, v in pairs], self.write_community)
        return dict(resp.varbinds)

    async def walk(self, root: Oid | str, bulk: int = 20) -> AsyncIterator[tuple[Oid, Any]]:
        root = Oid(root)
        cur = root
        while True:
            resp = await self.request(pdu.GET_BULK, [(cur, None)], error_status=0, error_index=bulk)
            if not resp.varbinds:
                return
            for oid, val in resp.varbinds:
                if isinstance(val, ber.Exception_) or not oid.startswith(root):
                    return
                yield oid, val
                cur = oid


class TrapReceiver(asyncio.DatagramProtocol):
    """Receives SNMPv2c traps and hands them to a callback(addr, trap_oid, varbinds)."""

    def __init__(self, callback: Callable[[tuple[str, int], Oid, list[tuple[Oid, Any]]], None],
                 community: bytes = b"public") -> None:
        self.callback, self.community = callback, community
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:  # type: ignore[override]
        self.transport = transport

    @property
    def port(self) -> int:
        return self.transport.get_extra_info("sockname")[1] if self.transport else 0

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        try:
            msg = Message.decode(data)
        except ber.BERError as exc:
            log.warning("bad trap from %s: %s", addr, exc)
            return
        if msg.pdu_type != pdu.TRAP_V2 or msg.community != self.community:
            return
        vbs = dict(msg.varbinds)
        trap_oid = vbs.get(pdu.SNMP_TRAP_OID_0)
        rest = [(o, v) for o, v in msg.varbinds if o not in (pdu.SYS_UPTIME_0, pdu.SNMP_TRAP_OID_0)]
        self.callback(addr, trap_oid, rest)

    @classmethod
    async def listen(cls, callback, host: str = "0.0.0.0", port: int = 1162) -> TrapReceiver:
        _, proto = await udp_endpoint(lambda: cls(callback), host, port)
        return proto
