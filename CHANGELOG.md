# Changelog

## 0.3.0 (2026-11-21)

> This repository is Tom Grozev's fork of
> [FrancescoMasaia/ha-soniox](https://github.com/FrancescoMasaia/ha-soniox),
> continued with significant changes. Entries below the 0.1.x history are
> from this fork.

### Added

- An Assist turn now ends as soon as Soniox endpoint detection sends `<end>`,
  instead of waiting for Home Assistant's VAD to close the audio stream. Two
  new options tune the detector: `max_endpoint_delay_ms` (500–3000 ms, default
  2000) and `endpoint_latency_adjustment_level` (0–3, default 0). With debug
  logging on, each realtime STT session logs the time from the last audio
  chunk to the transcript as `Soniox STT transcript … ms after the last audio
  chunk (endpoint detected)` or `(audio stream ended)`.
- Realtime STT can now send the names and aliases of entities exposed to
  Assist, plus area and floor names, to Soniox as context terms so commands
  such as "turn on the kitchen lights" recognise them. The `stt_home_context`
  option is off by default because it sends home data to Soniox.
- The config entry now offers a diagnostics download for bug reports. It
  contains the options, the region and the model and voice counts of the TTS
  catalog, with the API key redacted.

### Changed

- Voice satellites such as Voice PE keep their replies out of Home Assistant's
  ffmpeg step: the TTS entity now declares the preferred sample channel, sample
  size and bitrate options, and sends the sample rate to Soniox for every
  format, not only wav and pcm. An MP3 or AAC client's requested bitrate is
  forwarded too. Soniox output stays mono 16-bit, so a stereo request still
  gets mono.
- New installs and migrated legacy entries now use Soniox STT v6
  (`stt-rt-v6`, `stt-async-v6`); v5 is deprecated and removed on
  December 15, 2026. Existing entries keep their saved STT model, and Soniox
  routes v5 requests to v6 after that date.
- The TTS model dropdown no longer offers `tts-rt-v1`, which Soniox removed
  on August 31, 2026. Entries still saved on it keep working and are not
  changed.
- Setup now fetches the TTS model and voice catalogs concurrently, so a
  restart costs one round trip instead of two.

### Fixed

- A pipeline that replies with a TTS engine other than Soniox no longer opens
  a Soniox TTS connection when its Soniox speech-to-text starts. The
  speculative warm-up now runs only when an Assist pipeline pairs the Soniox
  speech-to-text entity with the Soniox text-to-speech entity of the same
  config entry, so a reply from another engine leaves no unused socket open.
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
- When Soniox is unreachable at startup, setup now asks Home Assistant to
  retry instead of leaving STT and TTS unavailable until the user reloads the
  integration. Once Soniox answers, the scheduled retry loads the entry.
- A 401 from Soniox at setup now starts a reauthentication flow, so the user
  can enter a new API key without removing and re-adding the entry.
