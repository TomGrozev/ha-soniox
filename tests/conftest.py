"""Shared fixtures for the Soniox integration tests."""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom integrations in all tests."""
    yield


class _FakeSonioxWebsocket:
    """Fake aiohttp websocket used by the Soniox integration.

    Records JSON-decoded outgoing messages on ``.sent`` (the integration uses
    ``send_json``) and yields scripted incoming messages. A ``dict`` in the
    script becomes a fake TEXT ``WSMessage`` carrying its JSON; any other item
    is yielded unchanged so tests can push CLOSED/ERROR-style fakes.
    """

    def __init__(self, incoming, connect_gate=None):
        self.incoming = list(incoming)
        self.sent = []
        self.url = None
        self.kwargs = None
        # Issue #8 pool tests: hold the WS "open" (await this before the fake
        # dial completes) so a warm-up can be scripted to stay in flight.
        self.connect_gate = connect_gate
        # Set when the pool explicitly closes the conn (borrowed teardown).
        self.closed = False
        self._it = None

    async def __aenter__(self):
        if self.connect_gate is not None:
            await self.connect_gate.wait()
        return self

    async def __aexit__(self, *exc):
        return False

    async def close(self):
        """Track an explicit client-side close (pool-managed connections)."""
        self.closed = True

    async def send_json(self, data, **_: object) -> None:
        self.sent.append(data)

    async def send_bytes(self, data, **_: object) -> None:
        self.sent.append(data)

    async def send_str(self, data, **_: object) -> None:
        self.sent.append(data)

    def __aiter__(self):
        self._it = iter(self.incoming)
        return self

    async def __anext__(self):
        if self._it is None:
            self._it = iter(self.incoming)
        while True:
            try:
                item = next(self._it)
            except StopIteration:
                raise StopAsyncIteration
            # A scripted asyncio.Event holds the receive loop open until the
            # test releases it — lets pool tests keep a warm conn "alive"
            # without inventing server frames.
            if isinstance(item, asyncio.Event):
                await item.wait()
                continue
            if isinstance(item, dict):
                return aiohttp.WSMessage(
                    aiohttp.WSMsgType.TEXT, json.dumps(item), None
                )
            return item


class _FakeWSConnect:
    """Async context manager and awaitable bridging either ``ws_connect`` call site.

    ``async with session.ws_connect(...) as ws`` enters the connection;
    ``ws = await session.ws_connect(...)`` awaits to the same fake object.
    """

    def __init__(self, ws: _FakeSonioxWebsocket):
        self._ws = ws

    def __await__(self):
        async def _go() -> _FakeSonioxWebsocket:
            if self._ws.connect_gate is not None:
                await self._ws.connect_gate.wait()
            return self._ws

        return _go().__await__()

    async def __aenter__(self) -> _FakeSonioxWebsocket:
        if self._ws.connect_gate is not None:
            await self._ws.connect_gate.wait()
        return self._ws

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def mock_soniox_ws(monkeypatch) -> object:
    """Monkeypatch ``aiohttp.ClientSession.ws_connect`` with a fake Soniox websocket.

    Use ``factory(incoming_script)`` to register a script (dicts become TEXT WS
    messages, other items pass through unchanged). The next ``ws_connect`` call
    uses the most recently registered script (or an empty one) and records
    ``url``/``kwargs`` on the connection. Inspect per-test results through
    ``factory.connections``.
    """

    registered: list[_FakeSonioxWebsocket] = []
    by_url: dict[str, _FakeSonioxWebsocket] = {}

    class _ScriptFactory:
        @property
        def connections(self) -> list[_FakeSonioxWebsocket]:
            return registered

        def __call__(
            self, incoming=None, url=None, connect_gate=None
        ) -> _FakeSonioxWebsocket:
            # Issue #8: URL-keyed scripts let a test script the STT and the
            # TTS (warm) websockets separately on one config entry. A plain
            # registration keeps the legacy single-script behaviour.
            ws = _FakeSonioxWebsocket(incoming or [], connect_gate=connect_gate)
            registered.append(ws)
            if url is not None:
                by_url[url] = ws
            return ws

    def fake_ws_connect(self, url, **kwargs) -> _FakeWSConnect:
        ws = by_url.get(url)
        if ws is None:
            ws = registered[-1] if registered else _FakeSonioxWebsocket([])
        ws.url = url
        ws.kwargs = kwargs
        return _FakeWSConnect(ws)

    monkeypatch.setattr(aiohttp.ClientSession, "ws_connect", fake_ws_connect)
    return _ScriptFactory()
