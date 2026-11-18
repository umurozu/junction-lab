"""SNMPv1/v2c message and PDU model (RFC 1157 / RFC 3416)."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import ber
from .ber import Oid

V1 = 0
V2C = 1

GET = 0xA0
GET_NEXT = 0xA1
RESPONSE = 0xA2
SET = 0xA3
GET_BULK = 0xA5
INFORM = 0xA6
TRAP_V2 = 0xA7

PDU_NAMES = {GET: "get", GET_NEXT: "getNext", RESPONSE: "response", SET: "set",
             GET_BULK: "getBulk", INFORM: "inform", TRAP_V2: "trapV2"}

# error-status values
NO_ERROR = 0
TOO_BIG = 1
NO_SUCH_NAME = 2
BAD_VALUE = 3
READ_ONLY = 4
GEN_ERR = 5
NO_ACCESS = 6
WRONG_TYPE = 7
WRONG_VALUE = 10
NOT_WRITABLE = 17

ERROR_NAMES = {0: "noError", 1: "tooBig", 2: "noSuchName", 3: "badValue", 4: "readOnly", 5: "genErr",
               6: "noAccess", 7: "wrongType", 10: "wrongValue", 17: "notWritable"}

# Well known OIDs
SYS_UPTIME_0 = Oid("1.3.6.1.2.1.1.3.0")
SNMP_TRAP_OID_0 = Oid("1.3.6.1.6.3.1.1.4.1.0")


@dataclass
class Message:
    pdu_type: int
    request_id: int
    varbinds: list[tuple[Oid, object]] = field(default_factory=list)
    community: bytes = b"public"
    version: int = V2C
    error_status: int = 0  # for GET_BULK this is non-repeaters
    error_index: int = 0  # for GET_BULK this is max-repetitions

    # ------------------------------------------------------------ encode
    def encode(self) -> bytes:
        vbs = ber.sequence(*(ber.sequence(ber.encode_oid(oid), ber.encode_value(val))
                             for oid, val in self.varbinds))
        pdu = ber.sequence(
            ber.encode_integer(self.request_id),
            ber.encode_integer(self.error_status),
            ber.encode_integer(self.error_index),
            vbs,
            tag=self.pdu_type,
        )
        return ber.sequence(ber.encode_integer(self.version), ber.encode_value(self.community), pdu)

    # ------------------------------------------------------------ decode
    @classmethod
    def decode(cls, data: bytes) -> Message:
        tag, payload, _ = ber.decode_tlv(data)
        if tag != ber.SEQUENCE:
            raise ber.BERError("message is not a SEQUENCE")
        parts = ber.decode_sequence(payload)
        if len(parts) != 3:
            raise ber.BERError("message must have version, community, pdu")
        (vt, vv), (ct, cv), (pt, pv) = parts
        if vt != ber.INTEGER or ct != ber.OCTET_STRING:
            raise ber.BERError("bad message header")
        version = ber.decode_value(vt, vv)
        if version not in (V1, V2C):
            raise ber.BERError(f"unsupported SNMP version {version}")
        if pt not in PDU_NAMES:
            raise ber.BERError(f"unsupported PDU 0x{pt:02x}")
        fields = ber.decode_sequence(pv)
        if len(fields) != 4:
            raise ber.BERError("PDU must have 4 fields")
        rid, err, idx = (ber.decode_value(t, v) for t, v in fields[:3])
        varbinds = []
        for vt2, vb in ber.decode_sequence(fields[3][1]):
            if vt2 != ber.SEQUENCE:
                raise ber.BERError("varbind is not a SEQUENCE")
            (ot, ov), (valt, valv) = ber.decode_sequence(vb)
            if ot != ber.OBJECT_IDENTIFIER:
                raise ber.BERError("varbind name is not an OID")
            varbinds.append((ber.decode_oid(ov), ber.decode_value(valt, valv)))
        return cls(pdu_type=pt, request_id=rid, varbinds=varbinds, community=cv, version=version,
                   error_status=err, error_index=idx)

    def __str__(self) -> str:
        vb = ", ".join(f"{o}={v!r}" for o, v in self.varbinds)
        err = f" err={ERROR_NAMES.get(self.error_status, self.error_status)}@{self.error_index}" \
            if self.pdu_type == RESPONSE and self.error_status else ""
        return f"{PDU_NAMES[self.pdu_type]}#{self.request_id}{err} [{vb}]"
