import pytest

from junctionlab.snmp import ber, pdu
from junctionlab.snmp.ber import Counter32, Gauge32, Oid, TimeTicks
from junctionlab.snmp.pdu import Message


@pytest.mark.parametrize("value,hexbytes", [
    (0, "020100"), (127, "02017f"), (128, "02020080"), (256, "02020100"),
    (-1, "0201ff"), (-128, "020180"), (-129, "0202ff7f"),
])
def test_integer_encoding_matches_x690(value, hexbytes):
    assert ber.encode_integer(value).hex() == hexbytes
    tag, payload, _ = ber.decode_tlv(bytes.fromhex(hexbytes))
    assert ber.decode_value(tag, payload) == value


def test_oid_encoding_known_vector():
    # 1.3.6.1.4.1.1206 -> 06 07 2b 06 01 04 01 89 36
    assert ber.encode_oid(Oid("1.3.6.1.4.1.1206")).hex() == "06072b060104018936"
    assert ber.decode_oid(bytes.fromhex("2b06010401893604")) == Oid("1.3.6.1.4.1.1206.4")


def test_long_length_form():
    payload = b"x" * 300
    enc = ber.tlv(ber.OCTET_STRING, payload)
    assert enc[:4] == bytes([0x04, 0x82, 0x01, 0x2C])
    assert ber.decode_tlv(enc)[1] == payload


def test_unsigned_types_roundtrip():
    for v in (Counter32(0), Counter32(2**32 - 1), Gauge32(128), TimeTicks(123456)):
        tag, payload, _ = ber.decode_tlv(ber.encode_value(v))
        out = ber.decode_value(tag, payload)
        assert out == v and type(out) is type(v)


def test_message_roundtrip_and_known_get_request():
    # snmpget -v2c -c public host 1.3.6.1.2.1.1.1.0 with request-id 1
    known = bytes.fromhex("302602010104067075626c6963a019020101020100020100300e300c06082b060102010101000500")
    msg = Message.decode(known)
    assert msg.pdu_type == pdu.GET and msg.community == b"public" and msg.request_id == 1
    assert msg.varbinds == [(Oid("1.3.6.1.2.1.1.1.0"), None)]
    assert msg.encode() == known


@pytest.mark.parametrize("garbage", [b"", b"\x30", b"\x30\x05\x02\x01", b"\x04\x00", b"\x30\x84\xff\xff\xff\xff"])
def test_malformed_input_raises_ber_error(garbage):
    with pytest.raises(ber.BERError):
        Message.decode(garbage)


def test_oid_ordering_is_lexicographic():
    assert Oid("1.3.6.1.2") < Oid("1.3.6.1.10")
    assert Oid("1.3.6.1") < Oid("1.3.6.1.0")
