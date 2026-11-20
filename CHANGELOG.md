# Changelog

## Unreleased

### Changed

- New installs and migrated legacy entries now use Soniox STT v6
  (`stt-rt-v6`, `stt-async-v6`); v5 is deprecated and removed on
  December 15, 2026. Existing entries keep their saved STT model, and Soniox
  routes v5 requests to v6 after that date.
- The TTS model dropdown no longer offers `tts-rt-v1`, which Soniox removed
  on August 31, 2026. Entries still saved on it keep working and are not
  changed.

### Fixed

- A Soniox realtime STT session that keeps its socket open without sending a
  final reply no longer hangs the Assist turn. The wait after the audio ends is
  now capped at 10 seconds; a stalled session returns an STT error and logs one
  line that names the Soniox STT session.
- Streaming TTS no longer stays silent until the whole LLM reply is in. Soniox's
  tts-rt-v2 model holds back text under about 80 characters until more text or
  `text_end` arrives, so a short opening sentence ("Let me check the garage
  sensor for you.") followed by a tool call played nothing until the answer was
  complete. When the text pauses for 250 ms at a sentence end (`.`, `!`, `?` or
  `…`, optionally followed by closing quotes or brackets), the integration now
  ends that Soniox stream and opens a fresh one for the next text. Text that
  arrives faster stays on one stream. With debug logging on, each early end
  logs `text paused at a sentence end; text_end on stream …`, and the
  `first audio frame … after first text` line shows the shorter wait.
