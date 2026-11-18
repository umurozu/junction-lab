"""JLP - Junction Link Protocol.

A compact binary protocol of the kind field devices use towards a center
(and that detector boxes use towards a controller). It runs over UDP here,
but the frame is self-delimiting and CRC protected, so the same encoding
works on an RS-485 serial line.

Frame layout (big endian)::

    0      2      3      4        6          8         10          10+N    12+N
    +------+------+------+--------+----------+---------+-----------+-------+
    | 'JL' | ver  | type |  seq   | deviceId | length  |  payload  | CRC16 |
    +------+------+------+--------+----------+---------+-----------+-------+

CRC16/CCITT-FALSE (poly 0x1021, init 0xFFFF) over header + payload.
A Wireshark Lua dissector is provided in tools/wireshark/jlp.lua.
"""

from __future__ import annotations

import enum
import struct
from dataclasses import dataclass

MAGIC = b"JL"
VERSION = 1
HEADER = struct.Struct(">2sBBHHH")  # magic, version, type, seq, device, length
CRC = struct.Struct(">H")
MAX_PAYLOAD = 1024


class MsgType(enum.IntEnum):
    STATUS = 0x01     # device -> center, periodic heartbeat + state
    DETECTOR = 0x02   # detector/camera -> device: vehicle actuations
    ALARM = 0x03      # device -> center
    COUNTS = 0x04     # device -> center, per-phase volumes
    COMMAND = 0x10    # center -> device
    ACK = 0x11        # device -> center


class Command(enum.IntEnum):
    SET_PATTERN = 1
    PREEMPT = 2
    INJECT_FAULT = 3
    CLEAR_FAULTS = 4
    PED_REQUEST = 5


class FrameError(ValueError):
    pass


def crc16_ccitt(data: bytes, crc: int = 0xFFFF) -> int:
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


@dataclass
class Frame:
    type: MsgType
    device: int
    payload: bytes = b""
    seq: int = 0

    def encode(self) -> bytes:
        if len(self.payload) > MAX_PAYLOAD:
            raise FrameError("payload too large")
        head = HEADER.pack(MAGIC, VERSION, int(self.type), self.seq & 0xFFFF, self.device, len(self.payload))
        body = head + self.payload
        return body + CRC.pack(crc16_ccitt(body))

    @classmethod
    def decode(cls, data: bytes) -> Frame:
        if len(data) < HEADER.size + CRC.size:
            raise FrameError("frame too short")
        magic, ver, typ, seq, dev, length = HEADER.unpack_from(data)
        if magic != MAGIC:
            raise FrameError("bad magic")
        if ver != VERSION:
            raise FrameError(f"unsupported version {ver}")
        end = HEADER.size + length
        if len(data) < end + CRC.size:
            raise FrameError("truncated payload")
        (crc,) = CRC.unpack_from(data, end)
        if crc != crc16_ccitt(data[:end]):
            raise FrameError("CRC mismatch")
        try:
            mtype = MsgType(typ)
        except ValueError as exc:
            raise FrameError(f"unknown type 0x{typ:02x}") from exc
        return cls(mtype, dev, bytes(data[HEADER.size:end]), seq)


# ---------------------------------------------------------------- payloads
STATUS = struct.Struct(">IBBBBHHHHHHH")
# uptime_s, mode, pattern, stage(1-based), interval, greens, yellows, reds, red_yellows, walks, alarms, cycle


@dataclass
class Status:
    uptime: int
    mode: int
    pattern: int
    stage: int
    interval: int
    greens: int
    yellows: int
    reds: int
    red_yellows: int
    walks: int
    alarms: int
    cycle: int

    def pack(self) -> bytes:
        return STATUS.pack(self.uptime, self.mode, self.pattern, self.stage, self.interval, self.greens,
                           self.yellows, self.reds, self.red_yellows, self.walks, self.alarms,
                           self.cycle & 0xFFFF)

    @classmethod
    def unpack(cls, b: bytes) -> Status:
        return cls(*STATUS.unpack_from(b))


def pack_detector(phase: int, count: int = 1, presence: bool = False) -> bytes:
    return struct.pack(">BBB", phase, count, int(presence))


def unpack_detector(b: bytes) -> tuple[int, int, bool]:
    phase, count, presence = struct.unpack_from(">BBB", b)
    return phase, count, bool(presence)


def pack_counts(counts: dict[int, int]) -> bytes:
    items = sorted(counts.items())
    return struct.pack(">B", len(items)) + b"".join(struct.pack(">BI", p, c & 0xFFFFFFFF) for p, c in items)


def unpack_counts(b: bytes) -> dict[int, int]:
    (n,) = struct.unpack_from(">B", b)
    return {p: c for p, c in (struct.unpack_from(">BI", b, 1 + i * 5) for i in range(n))}


def pack_alarm(alarm: int, phase: int, raised: bool) -> bytes:
    return struct.pack(">HBB", alarm, phase, int(raised))


def unpack_alarm(b: bytes) -> tuple[int, int, bool]:
    a, p, r = struct.unpack_from(">HBB", b)
    return a, p, bool(r)


def pack_command(cmd: Command, arg: int = 0, arg2: int = 0) -> bytes:
    return struct.pack(">BHH", int(cmd), arg, arg2)


def unpack_command(b: bytes) -> tuple[Command, int, int]:
    c, a, a2 = struct.unpack_from(">BHH", b)
    return Command(c), a, a2


def pack_ack(seq: int, ok: bool, reason: str = "") -> bytes:
    r = reason.encode()[:200]
    return struct.pack(">HBB", seq, int(ok), len(r)) + r


def unpack_ack(b: bytes) -> tuple[int, bool, str]:
    seq, ok, n = struct.unpack_from(">HBB", b)
    return seq, bool(ok), b[4:4 + n].decode(errors="replace")
