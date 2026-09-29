import asyncio

from ArmoredSync.service import TelegramSource


class _Reader:
    def __init__(self):
        self.client = self
        self.connected = False

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False


def test_historical_catchup_limit_stops_after_candidate_count(monkeypatch, tmp_path):
    monkeypatch.setenv("ARMORED_SYNC_SOURCE", "Teste")
    monkeypatch.setenv("ARMORED_SYNC_CATCHUP_LIMIT", "2")

    reader = _Reader()
    source = TelegramSource(tmp_path, reader)

    async def discover(_source):
        return [(10, "Teste")]

    async def topic_messages(_source, _topic_id):
        from types import SimpleNamespace

        for message_id in (101, 102, 103):
            yield SimpleNamespace(
                id=message_id,
                video=True,
                message=f"https://shopee.com.br/produto/{message_id}",
                entities=[],
            )

    source._discover_topics = discover
    source._topic_messages = topic_messages

    async def collect():
        return [item async for item in source.iter_historical_candidates_async()]

    candidates = asyncio.run(collect())

    assert [item.telegram_message_id for item in candidates] == ["101", "102"]
    assert source.historical_limit_reached is True
    assert reader.connected is True

    asyncio.run(reader.disconnect())
    assert reader.connected is False


def test_historical_catchup_limit_is_optional(monkeypatch, tmp_path):
    monkeypatch.delenv("ARMORED_SYNC_CATCHUP_LIMIT", raising=False)

    source = TelegramSource(tmp_path, _Reader())

    assert source.historical_collection_limited is False
    assert source.historical_limit_reached is False


def test_historical_catchup_limit_does_not_stop_fetch_next_path(monkeypatch, tmp_path):
    monkeypatch.setenv("ARMORED_SYNC_SOURCE", "Teste")
    monkeypatch.setenv("ARMORED_SYNC_CATCHUP_LIMIT", "2")

    reader = _Reader()
    source = TelegramSource(tmp_path, reader)

    async def discover(_source):
        return [(10, "Teste")]

    async def topic_messages(_source, _topic_id):
        from types import SimpleNamespace

        for message_id in (201, 202, 203):
            yield SimpleNamespace(
                id=message_id,
                video=True,
                message=f"https://shopee.com.br/produto/{message_id}",
                entities=[],
            )

    source._discover_topics = discover
    source._topic_messages = topic_messages

    async def collect():
        first = await source.fetch_next_async()
        second = await source.fetch_next_async()
        third = await source.fetch_next_async()
        return first, second, third

    first, second, third = asyncio.run(collect())

    # Certification limits belong to Coordinator; Sync discovery behaves like production.
    assert [first.telegram_message_id, second.telegram_message_id, third.telegram_message_id] == ["201", "202", "203"]
    assert source.historical_limit_reached is False
    assert source.historical_scan_exhausted is False
    assert reader.connected is True

    asyncio.run(reader.disconnect())


def test_zero_catchup_limit_means_unlimited(monkeypatch, tmp_path):
    monkeypatch.setenv("ARMORED_SYNC_CATCHUP_LIMIT", "0")

    source = TelegramSource(tmp_path, _Reader())

    assert source.historical_collection_limited is False
    assert source.historical_limit_reached is False
    assert source._historical_limit is None
}
