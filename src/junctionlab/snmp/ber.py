"""Minimal ASN.1 BER codec for SNMPv1/v2c.

Written from scratch (no pysnmp) so that every byte on the wire is understood.
Only the subset of BER that SNMP uses is implemented: definite lengths,
single-byte tags, primitive INTEGER / OCTET STRING / NULL / OID and the
SNMP application types, plus constructed SEQUENCE / context PDUs.
"""

from __future__ import annotations

from dataclasses import dataclass

# Universal tags
INTEGER = 0x02
OCTET_STRING = 0x04
NULL = 0x05
OBJECT_IDENTIFIER = 0x06
SEQUENCE = 0x30
# SNMP application tags (RFC 2578)
IP_ADDRESS = 0x40
COUNTER32 = 0x41
GAUGE32 = 0x42
TIMETICKS = 0x43
OPAQUE = 0x44
COUNTER64 = 0x46
# v2c varbind exceptions (RFC 3416)
NO_SUCH_OBJECT = 0x80
NO_SUCH_INSTANCE = 0x81
END_OF_MIB_VIEW = 0x82


class BERError(ValueError):
    """Raised for malformed BER input."""


class Oid(tuple):
    """Object identifier, comparable lexicographically like the MIB tree."""

    def __new__(cls, value: str | tuple[int, ...] | list[int] | Oid) -> Oid:
        if isinstance(value, str):
            value = tuple(int(x) for x in value.strip(".").split(".") if x != "")
        return super().__new__(cls, tuple(value))

    def __str__(self) -> str:
        return ".".join(str(x) for x in self)

    def __repr__(self) -> str:
        return f"Oid('{self}')"

    def __add__(self, other) -> Oid:  # type: ignore[override]
        return Oid(tuple(self) + tuple(Oid(other) if isinstance(other, str) else other))

    def startswith(self, prefix: Oid) -> bool:
        return tuple(self[: len(prefix)]) == tuple(prefix)


class Counter32(int):
    tag = COUNTER32


class Gauge32(int):
    tag = GAUGE32


class TimeTicks(int):
    tag = TIMETICKS


class Counter64(int):
    tag = COUNTER64


@dataclass(frozen=True)
class IpAddress:
    value: str
    tag = IP_ADDRESS


@dataclass(frozen=True)
class Exception_:
    """v2c varbind exception value (noSuchObject, noSuchInstance, endOfMibView)."""

    tag: int

    def __repr__(self) -> str:
        return {NO_SUCH_OBJECT: "noSuchObject", NO_SUCH_INSTANCE: "noSuchInstance",
                END_OF_MIB_VIEW: "endOfMibView"}.get(self.tag, f"Exception({self.tag})")


NoSuchObject = Exception_(NO_SUCH_OBJECT)
NoSuchInstance = Exception_(NO_SUCH_INSTANCE)
EndOfMibView = Exception_(END_OF_MIB_VIEW)


# ---------------------------------------------------------------- encoding

def encode_length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def tlv(tag: int, payload: bytes) -> bytes:
    return bytes([tag]) + encode_length(len(payload)) + payload


def encode_integer(value: int, tag: int = INTEGER) -> bytes:
    length = max(1, (value.bit_length() + 8) // 8)
    raw = value.to_bytes(length, "big", signed=True)
    # strip redundant leading bytes (keep two's complement sign)
    while len(raw) > 1 and ((raw[0] == 0x00 and raw[1] < 0x80) or (raw[0] == 0xFF and raw[1] >= 0x80)):
        raw = raw[1:]
    return tlv(tag, raw)


def encode_unsigned(value: int, tag: int, bits: int = 32) -> bytes:
    if not 0 <= value < (1 << bits):
        raise BERError(f"value {value} out of range for {bits}-bit unsigned")
    raw = value.to_bytes(value.bit_length() // 8 + 1, "big")
    return tlv(tag, raw)


def encode_oid(oid: Oid) -> bytes:
    if len(oid) < 2:
        raise BERError("OID needs at least two arcs")
    arcs = [oid[0] * 40 + oid[1], *oid[2:]]
    out = bytearray()
    for arc in arcs:
        chunk = [arc & 0x7F]
        arc >>= 7
        while arc:
            chunk.append(0x80 | (arc & 0x7F))
            arc >>= 7
        out.extend(reversed(chunk))
    return tlv(OBJECT_IDENTIFIER, bytes(out))


def encode_value(value) -> bytes:
    """Encode a Python value as the matching SNMP type."""
    if value is None:
        return tlv(NULL, b"")
    if isinstance(value, Exception_):
        return tlv(value.tag, b"")
    if isinstance(value, Counter64):
        return encode_unsigned(int(value), COUNTER64, 64)
    if isinstance(value, (Counter32, Gauge32, TimeTicks)):
        return encode_unsigned(int(value), value.tag)
    if isinstance(value, bool):
        return encode_integer(int(value))
    if isinstance(value, int):
        return encode_integer(value)
    if isinstance(value, Oid):
        return encode_oid(value)
    if isinstance(value, IpAddress):
        return tlv(IP_ADDRESS, bytes(int(x) for x in value.value.split(".")))
    if isinstance(value, str):
        return tlv(OCTET_STRING, value.encode())
    if isinstance(value, (bytes, bytearray)):
        return tlv(OCTET_STRING, bytes(value))
    raise BERError(f"cannot encode {type(value).__name__}")


def sequence(*items: bytes, tag: int = SEQUENCE) -> bytes:
    return tlv(tag, b"".join(items))


# ---------------------------------------------------------------- decoding

def decode_tlv(data: bytes, pos: int = 0) -> tuple[int, bytes, int]:
    """Return (tag, payload, next_pos)."""
    if pos + 2 > len(data):
        raise BERError("truncated TLV header")
    tag = data[pos]
    if tag & 0x1F == 0x1F:
        raise BERError("multi-byte tags are not used by SNMP")
    first = data[pos + 1]
    pos += 2
    if first < 0x80:
        length = first
    else:
        n = first & 0x7F
        if n == 0 or n > 4:
            raise BERError("indefinite or oversized length")
        if pos + n > len(data):
            raise BERError("truncated length")
        length = int.from_bytes(data[pos:pos + n], "big")
        pos += n
    end = pos + length
    if end > len(data):
        raise BERError("truncated value")
    return tag, data[pos:end], end


def decode_sequence(payload: bytes) -> list[tuple[int, bytes]]:
    items, pos = [], 0
    while pos < len(payload):
        tag, value, pos = decode_tlv(payload, pos)
        items.append((tag, value))
    return items


def decode_oid(payload: bytes) -> Oid:
    if not payload:
        raise BERError("empty OID")
    arcs, acc = [], 0
    for b in payload:
        acc = (acc << 7) | (b & 0x7F)
        if not b & 0x80:
            arcs.append(acc)
            acc = 0
    if payload[-1] & 0x80:
        raise BERError("unterminated OID arc")
    first = arcs[0]
    head = [min(first // 40, 2), first - 40 * min(first // 40, 2)]
    return Oid(head + arcs[1:])


def decode_value(tag: int, payload: bytes):
    if tag == INTEGER:
        return int.from_bytes(payload, "big", signed=True) if payload else 0
    if tag == OCTET_STRING:
        return bytes(payload)
    if tag == NULL:
        return None
    if tag == OBJECT_IDENTIFIER:
        return decode_oid(payload)
    if tag == IP_ADDRESS:
        return IpAddress(".".join(str(b) for b in payload))
    if tag in (COUNTER32, GAUGE32, TIMETICKS, COUNTER64):
        cls = {COUNTER32: Counter32, GAUGE32: Gauge32, TIMETICKS: TimeTicks, COUNTER64: Counter64}[tag]
        return cls(int.from_bytes(payload, "big"))
    if tag in (NO_SUCH_OBJECT, NO_SUCH_INSTANCE, END_OF_MIB_VIEW):
        return Exception_(tag)
    raise BERError(f"unsupported tag 0x{tag:02x}")
