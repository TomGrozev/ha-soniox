"""Tests for StreamJoiner (custom_components/soniox/audio_join.py).

Headers are synthetic and built here from the documented container layouts.
"""

import struct

from custom_components.soniox.audio_join import StreamJoiner


def chunk(cid: bytes, payload: bytes) -> bytes:
    pad = b"\x00" if len(payload) & 1 else b""
    return cid + struct.pack("<I", len(payload)) + payload + pad


def wav_header(data_size: int, extra: bytes = b"", riff_size: int | None = None) -> bytes:
    body = b"WAVE" + extra
    body += b"fmt " + struct.pack("<I", 16) + b"\x00" * 16
    body += b"data" + struct.pack("<I", data_size)
    size = min(len(body) + data_size, 0xFFFFFFFF) if riff_size is None else riff_size
    return b"RIFF" + struct.pack("<I", size) + body


def flac_streaminfo(total_samples: int = 0, md5: bytes = b"\x00" * 16) -> bytes:
    si = bytearray(34)
    si[10:18] = ((44100 << 44) | (2 << 41) | (16 << 36) | total_samples).to_bytes(8, "big")
    si[18:34] = md5
    return bytes(si)


def flac_block(block_type: int, payload: bytes, last: bool) -> bytes:
    return bytes([block_type | (0x80 if last else 0)]) + len(payload).to_bytes(3, "big") + payload


def id3(size: int, footer: bool = False) -> bytes:
    syncsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    tag = b"ID3" + b"\x04\x00" + bytes([0x10 if footer else 0]) + syncsafe
    tag += b"\x00" * size + (b"\x00" * 10 if footer else b"")
    return tag


def with_lead(fmt: str, lead: bytes) -> StreamJoiner:
    """A joiner whose lead segment has already emitted audio."""
    j = StreamJoiner(fmt)
    j.start_segment()
    assert j.feed(lead)
    j.end_segment()
    j.start_segment()
    return j


# --- wav -------------------------------------------------------------------


def test_wav_lead_rewrites_finite_sizes():
    header = wav_header(data_size=1000)
    j = StreamJoiner("wav")
    j.start_segment()
    out = j.feed(header + b"AUDIO")
    expected = bytearray(header)
    expected[4:8] = b"\xff" * 4
    expected[-4:] = b"\xff" * 4
    assert out == bytes(expected) + b"AUDIO"


def test_wav_lead_keeps_sentinel_and_zero_sizes():
    header = wav_header(data_size=0xFFFFFFFF, riff_size=0xFFFFFFFF)
    j = StreamJoiner("wav")
    j.start_segment()
    assert j.feed(header + b"A") == header + b"A"

    header = wav_header(data_size=0)
    j = StreamJoiner("wav")
    j.start_segment()
    out = j.feed(header)
    assert out[-4:] == b"\x00\x00\x00\x00"  # data size 0 is kept
    assert out[:8] != header[:8]  # riff size was finite, so rewritten


def test_wav_continuation_strips_header_with_extra_chunk():
    j = with_lead("wav", wav_header(0xFFFFFFFF) + b"LEAD")
    header = wav_header(4, extra=chunk(b"LIST", b"info"))
    assert j.feed(header + b"AUDIO2") == b"AUDIO2"


def test_wav_header_split_across_one_byte_chunks():
    j = with_lead("wav", wav_header(0xFFFFFFFF) + b"LEAD")
    header = wav_header(4, extra=chunk(b"LIST", b"info"))
    out = b"".join(j.feed(header[i : i + 1]) for i in range(len(header)))
    assert out == b""
    assert j.feed(b"AUDIO2") == b"AUDIO2"


def test_wav_lead_header_split_across_one_byte_chunks():
    header = wav_header(1000)
    j = StreamJoiner("wav")
    j.start_segment()
    out = b"".join(j.feed(header[i : i + 1]) for i in range(len(header)))
    expected = bytearray(header)
    expected[4:8] = b"\xff" * 4
    expected[-4:] = b"\xff" * 4
    assert out == bytes(expected)


def test_wav_continuation_without_magic_passes_through():
    j = with_lead("wav", wav_header(0xFFFFFFFF) + b"LEAD")
    assert j.feed(b"RAWBYTES") == b"RAWBYTES"


def test_incomplete_continuation_header_flushes_empty():
    j = with_lead("wav", wav_header(0xFFFFFFFF) + b"LEAD")
    assert j.feed(b"RIFF") == b""
    assert j.end_segment() == b""
    # the dropped fragment must not pollute the next segment
    j.start_segment()
    assert j.feed(wav_header(0xFFFFFFFF) + b"X") == b"X"


# --- flac ------------------------------------------------------------------


def test_flac_continuation_strips_all_metadata():
    j = with_lead("flac", b"fLaC" + flac_block(0, flac_streaminfo(), True) + b"FRAME0")
    header = b"fLaC"
    header += flac_block(0, flac_streaminfo(), False)
    header += flac_block(4, b"vendor", True)  # VORBIS_COMMENT, last
    assert j.feed(header + b"FRAME1") == b"FRAME1"


def test_flac_lead_zeroes_nonzero_streaminfo():
    si = flac_streaminfo(total_samples=123456, md5=b"\xaa" * 16)
    header = b"fLaC" + flac_block(0, si, True)
    j = StreamJoiner("flac")
    j.start_segment()
    out = j.feed(header + b"FRAME")
    expected = bytearray(header)
    expected[21] &= 0xF0
    expected[22:26] = b"\x00" * 4
    expected[26:42] = b"\x00" * 16
    assert out == bytes(expected) + b"FRAME"


def test_flac_lead_keeps_zero_streaminfo():
    header = b"fLaC" + flac_block(0, flac_streaminfo(), True)
    j = StreamJoiner("flac")
    j.start_segment()
    assert j.feed(header + b"FRAME") == header + b"FRAME"


def test_flac_lead_split_across_one_byte_chunks():
    si = flac_streaminfo(total_samples=99, md5=b"\x01" * 16)
    header = b"fLaC" + flac_block(0, si, True)
    j = StreamJoiner("flac")
    j.start_segment()
    out = b"".join(j.feed(header[i : i + 1]) for i in range(len(header)))
    expected = bytearray(header)
    expected[21] &= 0xF0
    expected[22:26] = b"\x00" * 4
    expected[26:42] = b"\x00" * 16
    assert out == bytes(expected)


# --- mp3 / aac -------------------------------------------------------------


def test_mp3_continuation_strips_id3():
    j = with_lead("mp3", b"LEADFRAMES")
    assert j.feed(id3(20) + b"FRAMES") == b"FRAMES"


def test_mp3_continuation_strips_id3_with_footer():
    j = with_lead("mp3", b"LEADFRAMES")
    tag = id3(20, footer=True)
    assert j.feed(tag + b"FRAMES") == b"FRAMES"


def test_mp3_continuation_without_id3_passes_through():
    j = with_lead("mp3", b"LEADFRAMES")
    assert j.feed(b"\xff\xfb\x90\x00FRAMES") == b"\xff\xfb\x90\x00FRAMES"


def test_aac_continuation_strips_id3():
    j = with_lead("aac", b"LEADFRAMES")
    assert j.feed(id3(10) + b"FRAMES") == b"FRAMES"


# --- pcm / unknown / lead bookkeeping --------------------------------------


def test_pcm_passthrough():
    j = StreamJoiner("pcm_s16le")
    for seg in (b"\x00\x01\x02\x03", b"RIFFnotreally", b""):
        j.start_segment()
        assert j.feed(seg) == seg
        assert j.end_segment() == b""


def test_unknown_format_passthrough():
    j = StreamJoiner("opus")
    j.start_segment()
    assert j.feed(b"ID3junk") == b"ID3junk"


def test_zero_audio_first_segment_does_not_take_lead():
    j = StreamJoiner("wav")
    j.start_segment()
    assert j.feed(b"") == b""
    assert j.end_segment() == b""

    j.start_segment()
    header = wav_header(0xFFFFFFFF)
    out = j.feed(header + b"A")
    assert out == header + b"A"  # second segment acted as the lead


def test_lead_end_segment_returns_held_bytes_unchanged():
    header = wav_header(0xFFFFFFFF)
    j = StreamJoiner("wav")
    j.start_segment()
    assert j.feed(header[:10]) == b""
    assert j.end_segment() == header[:10]
