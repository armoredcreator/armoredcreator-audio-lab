import asyncio

from pathlib import Path

from ArmoredSync.service import TelegramReader


class _FakeSession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeClient:
    def __init__(self, *, fail_start=False):
        self.fail_start = fail_start
        self.connected = False
        self.disconnect_calls = 0
        self.session = _FakeSession()

    def is_connected(self):
        return self.connected

    async def start(self):
        if self.fail_start:
            raise ConnectionError("telegram unavailable")
        self.connected = True

    async def disconnect(self):
        self.disconnect_calls += 1
        self.connected = False


def _reader(factory):
    reader = TelegramReader.__new__(TelegramReader)
    reader._session = Path("test-session")
    reader._api_id = 1
    reader._api_hash = "hash"
    reader._TelegramClient = factory
    reader.client = factory()
    return reader


def test_failed_telegram_start_closes_session_before_next_reconnect():
    created = [
        _FakeClient(fail_start=True),
        _FakeClient(fail_start=True),
        _FakeClient(fail_start=False),
    ]
    pool = list(created)

    def factory(*_args):
        return pool.pop(0)

    reader = _reader(factory)

    async def scenario():
        first_error = None
        try:
            await reader.connect()
        except ConnectionError as exc:
            first_error = exc
        assert first_error is not None
        # The initial client was consumed by _reader(); the failed reconnect
        # consumes the second client. The final client is created on the next
        # explicit connect attempt.
        assert pool == [created[2]]
        await reader.connect()
        assert pool == []

    asyncio.run(scenario())

    # The two clients that failed/preceded the final reconnect were closed
    # before their SQLite session files could remain owned by stale objects.
    assert created[0].session.closed is True
    assert created[1].session.closed is True
    assert created[2].session.closed is False
    assert reader.client.is_connected() is True


def test_disconnect_closes_session_even_when_client_reports_not_connected():
    reader = TelegramReader.__new__(TelegramReader)
    reader.client = _FakeClient()
    reader.client.session = _FakeSession()

    asyncio.run(reader.disconnect())

    assert reader.client.disconnect_calls == 1
    assert reader.client.session.closed is True
