# Changelog

## Unreleased

### Fixed

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
