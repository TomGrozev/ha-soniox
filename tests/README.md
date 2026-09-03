# Tests

Offline test harness for the Soniox integration (GitHub issue #2).

## Install

System Python often lacks C headers, so use a uv-managed CPython (ships full
headers; `lru-dict` — an HA 2025.12 dependency — needs them to build). Keep the
venv **out of the repo**:

```sh
uv venv --python-preference only-managed --python 3.13 ../ha-soniox-tests/.venv
uv pip install --python ../ha-soniox-tests/.venv/bin/python -r requirements_test.txt
```

## Run

```sh
../ha-soniox-tests/.venv/bin/python -m pytest -q
```

From the repo root (`pytest.ini` sets `testpaths = tests`, `asyncio_mode = auto`,
`pythonpath = .`).

## Layers

- **HTTP** — phacc's `aioclient_mock` fixture. With it active, any request to an
  unmocked URL raises, so unmocked network access fails loudly.
- **WebSocket** — `mock_soniox_ws` fixture (tests/conftest.py). Call
  `mock_soniox_ws([msg1, msg2])` to script the incoming side (`dict` →
  `WSMsgType.TEXT` with JSON `data`; other objects, e.g. `CLOSED`, pass
  through). The fake records URL/kwargs and every JSON-decoded outgoing
  message; assert on `.sent` / `mock_soniox_ws.connections`.
- **Payloads** — `tests/fixtures/models_payload.json` (GET /v1/tts-models) and
  `voices_payload.json` (GET /v1/voices). Field shapes follow the Soniox API
  reference and soniox-python SDK types: models carry per-model
  `supports_speed_adjustment` / `speed_min` / `speed_max` and a nested `voices`
  array; project voices carry per-model `status` (`ready` / …) for filtering.
