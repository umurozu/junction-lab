import pytest

from junctionlab.proto import frame as jlp
from junctionlab.proto.frame import Frame, FrameError, MsgType


def test_crc16_ccitt_false_check_value():
    assert jlp.crc16_ccitt(b"123456789") == 0x29B1  # standard check value


def test_frame_roundtrip():
    st = jlp.Status(3600, 2, 254, 1, 1, 0b0101, 0, 0b1010, 0, 0b0001, 0, 42)
    f = Frame(MsgType.STATUS, device=7, payload=st.pack(), seq=513)
    out = Frame.decode(f.encode())
    assert out == f
    assert jlp.Status.unpack(out.payload) == st


def test_corrupted_frame_rejected():
    raw = bytearray(Frame(MsgType.DETECTOR, 1, jlp.pack_detector(2, 3)).encode())
    raw[11] ^= 0x01
    with pytest.raises(FrameError, match="CRC"):
        Frame.decode(bytes(raw))


@pytest.mark.parametrize("raw", [b"", b"XX" + bytes(10), b"JL\x09" + bytes(9)])
def test_bad_headers(raw):
    with pytest.raises(FrameError):
        Frame.decode(raw)


def test_payload_helpers():
    assert jlp.unpack_counts(jlp.pack_counts({1: 10, 3: 70000})) == {1: 10, 3: 70000}
    assert jlp.unpack_alarm(jlp.pack_alarm(2, 4, True)) == (2, 4, True)
    assert jlp.unpack_command(jlp.pack_command(jlp.Command.PREEMPT, 2)) == (jlp.Command.PREEMPT, 2, 0)
    assert jlp.unpack_ack(jlp.pack_ack(9, False, "nope")) == (9, False, "nope")
