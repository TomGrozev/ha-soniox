# Soniox

Speech-to-text and text-to-speech for Home Assistant by **Tom Grozev**,
powered by [Soniox](https://soniox.com). 60+ languages, low-latency streaming.

> **Disclaimer**: This is an unofficial project and is not affiliated with,
> endorsed by, or maintained by Soniox.

## Highlights

- Real-time STT (`stt-rt-v6`) with Soniox endpoint detection, so the Assist
  turn ends as soon as you stop speaking.
- Optional context terms: entity names, aliases, areas and floors, so commands
  such as "turn on the kitchen lights" recognise your devices.
- Streaming TTS (`tts-rt-v2`) with 28 voices; audio starts before the reply is
  finished, every voice speaks every language.
- Satellite-friendly audio: preferred sample rate, channel and bitrate are
  declared, so Voice PE and similar clients skip Home Assistant's ffmpeg step.
- Diagnostics download for bug reports (API key redacted), reauthentication on
  a rejected API key, and automatic setup retry when Soniox is unreachable.
- Per-call voice and audio-format overrides.
- No external Python dependencies, built on Home Assistant's bundled
  `aiohttp`.

## Quick setup

1. Install via HACS, restart Home Assistant.
2. **Settings → Devices & Services → Add Integration → Soniox**.
3. Paste an API key from [console.soniox.com](https://console.soniox.com)
   and pick the matching region (US, EU, or Japan).
4. **Settings → Voice assistants** → set both STT and TTS engines to **Soniox**.

Full docs and configuration options:
[github.com/TomGrozev/ha-soniox](https://github.com/TomGrozev/ha-soniox).

<img src="https://raw.githubusercontent.com/TomGrozev/ha-soniox/main/custom_components/soniox/brand/logo.png" alt="Soniox logo" width="200">
