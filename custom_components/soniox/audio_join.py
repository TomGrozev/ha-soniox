"""Join the audio of consecutive Soniox TTS streams into one container stream.

Soniox retires a stream after a few seconds without text and offers no
per-stream keepalive, so a pause in the reply (e.g. a tool call) means a new
stream with a fresh ``stream_id`` and its own container header. Home Assistant
expects one byte stream per TTS request, so the continuation headers must go --
and the lead header's length fields must be marked unknown, because the joined
length is not known when that header is sent.

Only the header of each stream is touched; audio frames are never inspected.
"""

from __future__ import annotations

_MAX_HEADER = 1 << 20  # a header longer than this is malformed; stop buffering

# A continuation must start with its container's magic or it is passed through
# untouched (Soniox may already send headerless frames).
_MAGIC = {"wav": b"RIFF", "flac": b"fLaC", "mp3": b"ID3", "aac": b"ID3"}


def _wav_header_len(buf: bytes) -> int | None:
    """Length of the RIFF header through the `data` chunk header, or None if the
    buffer is short. Layout: RIFF(0-3) size(4-7) WAVE(8-11), then chunks of
    id(0-3) size(4-7) data, word-aligned.
    """
    if len(buf) < 12:
        return None
    pos = 12
    while True:
        if len(buf) < pos + 8:
            return None
        size = int.from_bytes(buf[pos + 4 : pos + 8], "little")
        if buf[pos : pos + 4] == b"data":
            return pos + 8
        pos += 8 + size + (size & 1)


def _patch_wav_lead(header: bytes) -> bytes:
    """Rewrite a finite RIFF or `data` size to 0xFFFFFFFF.

    The joined length is unknown when the lead header goes out, and 0 would make
    readers such as Python's `wave` module see zero frames.
    """
    out = bytearray(header)
    for off in (4, len(out) - 4):
        if int.from_bytes(out[off : off + 4], "little") not in (0, 0xFFFFFFFF):
            out[off : off + 4] = b"\xff\xff\xff\xff"
    return bytes(out)


# ponytail: continuation FLAC frames keep their own frame numbers, which restart
# at 0 per stream. Sequential decoders play the joined audio byte-exact, but
# ffmpeg warns about non-monotonic timestamps. Renumbering means finding frame
# boundaries and rewriting each frame header's number plus its CRC-8 and CRC-16.
def _flac_header_len(buf: bytes) -> int | None:
    """Length of the FLAC marker plus all metadata blocks, or None if short.
    Layout: fLaC(0-3), then blocks of flags+type(0) length(1-3, big-endian),
    the last-block flag being bit 7 of the flags byte.
    """
    if len(buf) < 4:
        return None
    pos = 4
    while True:
        if len(buf) < pos + 4:
            return None
        last = buf[pos] & 0x80
        end = pos + 4 + int.from_bytes(buf[pos + 1 : pos + 4], "big")
        if len(buf) < end:
            return None
        pos = end
        if last:
            return pos


def _patch_flac_lead(header: bytes) -> bytes:
    """Zero STREAMINFO total_samples and MD5 when non-zero.

    Offsets in the file: the 34-byte STREAMINFO block starts at 8; total_samples
    is the low 36 bits at 21-25 (4 bits of byte 21, bytes 22-25) and MD5 is
    26-41. The joined length is unknown and a stale MD5 would fail verification.
    """
    out = bytearray(header)
    if (out[4] & 0x7F) == 0 and int.from_bytes(out[5:8], "big") == 34:
        if (out[21] & 0x0F) or any(out[22:26]):
            out[21] &= 0xF0
            out[22:26] = b"\x00" * 4
        if any(out[26:42]):
            out[26:42] = b"\x00" * 16
    return bytes(out)


def _id3_header_len(buf: bytes) -> int | None:
    """Length of an ID3v2 tag, or None if short. Layout: ID3(0-2) version(3-4)
    flags(5) syncsafe size(6-9) tag, plus a 10-byte footer when flag 0x10 is set.
    """
    if len(buf) < 10:
        return None
    size = (
        ((buf[6] & 0x7F) << 21)
        | ((buf[7] & 0x7F) << 14)
        | ((buf[8] & 0x7F) << 7)
        | (buf[9] & 0x7F)
    )
    end = 10 + size + (10 if buf[5] & 0x10 else 0)
    return end if len(buf) >= end else None


_SCAN = {
    "wav": _wav_header_len,
    "flac": _flac_header_len,
    "mp3": _id3_header_len,
    "aac": _id3_header_len,
}
_PATCH = {"wav": _patch_wav_lead, "flac": _patch_flac_lead}


class StreamJoiner:
    """Join the audio of consecutive Soniox TTS streams into one container stream."""

    def __init__(self, audio_format: str) -> None:
        # pcm_s16le is headerless and unknown formats are left alone.
        self._kind = audio_format if audio_format in _MAGIC else None
        self._lead_emitted = False
        self._buf = b""
        self._is_lead = False
        self._header_done = False

    def start_segment(self) -> None:
        """Call before the first feed() of each Soniox stream."""
        self._is_lead = not self._lead_emitted
        # A lead mp3/aac header is kept as-is, so there is nothing to buffer.
        self._header_done = self._kind is None or (
            self._is_lead and self._kind in ("mp3", "aac")
        )
        self._buf = b""

    def feed(self, chunk: bytes) -> bytes:
        """Bytes to yield now; b"" while a header is still being buffered."""
        if self._header_done:
            if chunk:
                self._lead_emitted = True
            return chunk

        self._buf += chunk
        if not self._is_lead:
            magic = _MAGIC[self._kind]
            if len(self._buf) < len(magic):
                return b"" if magic.startswith(self._buf) else self._flush()
            if not self._buf.startswith(magic):
                return self._flush()

        length = _SCAN[self._kind](self._buf)
        if length is None:
            if len(self._buf) > _MAX_HEADER:
                return self._flush()
            return b""

        header, rest = self._buf[:length], self._buf[length:]
        if self._is_lead:
            rest = _PATCH.get(self._kind, lambda h: h)(header) + rest
        # A continuation's header is dropped so its audio continues the lead.
        self._header_done = True
        self._buf = b""
        if rest:
            self._lead_emitted = True
        return rest

    def end_segment(self) -> bytes:
        """That stream's audio is finished; returns any held bytes."""
        held, self._buf = self._buf, b""
        self._header_done = True
        if not held:
            return b""
        # A lead's held bytes are real (if incomplete) data. A continuation's are
        # a header fragment, which is not audio and must not be emitted.
        if self._is_lead:
            self._lead_emitted = True
            return held
        return b""

    def _flush(self) -> bytes:
        """Give up on parsing: emit the buffer as-is."""
        out, self._buf = self._buf, b""
        self._header_done = True
        if out:
            self._lead_emitted = True
        return out
