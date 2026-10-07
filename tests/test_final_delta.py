from __future__ import annotations

import asyncio
from types import SimpleNamespace

import numpy as np

from ArmoredStudio.analysis.audio_intelligence import AudioIntelligence, AudioMode
from ArmoredSync.service import MultiTelegramSource, SyncMessage
from ArmoredVision.service import ArmoredVision
from armored_core.services import VisionUnresolvedError


def test_audio_intelligence_no_audio_is_explicit():
    result = AudioIntelligence._classify_samples(np.zeros(16000, dtype=np.float32), 1.0)
    assert result.mode == AudioMode.NO_AUDIO


def test_audio_intelligence_tonal_source_is_music_only():
    rate = AudioIntelligence.SAMPLE_RATE
    t = np.arange(rate * 2, dtype=np.float32) / rate
    samples = (0.20 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    result = AudioIntelligence._classify_samples(samples, 2.0)
    assert result.mode == AudioMode.MUSIC_ONLY


class _FakeSource:
    def __init__(self, source_id: str, messages: list[str]):
        self.source_id = source_id
        self._messages = list(messages)
        self._historical_complete = False
        self._historical_scan_exhausted = False
        self._failed = False
        self.calls = 0

    @property
    def historical_materialization_failed(self):
        return self._failed

    @property
    def historical_scan_exhausted(self):
        return self._historical_scan_exhausted

    def is_historical_complete(self):
        return self._historical_complete

    async def fetch_next_async(self):
        self.calls += 1
        if self._messages:
            value = self._messages.pop(0)
            return SyncMessage(telegram_message_id=value, source_id=self.source_id)
        self._historical_scan_exhausted = True
        return None

    def complete_historical_sync(self):
        assert self._historical_scan_exhausted
        self._historical_complete = True

    def mark_ingested(self, _message_id):
        pass

    def mark_materialization_failed(self):
        self._failed = True

    def reset_historical_scan(self):
        pass


class _FakeDB:
    def __init__(self):
        self.completed = 0

    def complete_historical_sync(self):
        self.completed += 1


def test_multisource_historical_is_strictly_sequential():
    source1 = _FakeSource("source-1", ["101", "102"])
    source2 = _FakeSource("source-2", ["201"])
    db = _FakeDB()

    multi = MultiTelegramSource.__new__(MultiTelegramSource)
    multi.db = db
    multi.sources = (source1, source2)
    multi._cursor = 0
    multi._last_source = None
    multi._pending_source = None
    multi._pending_message = None
    multi._pending_checkpoints = {}

    first = asyncio.run(multi.fetch_next_async())
    assert first is not None and first.source_id == "source-1"
    assert source2.calls == 0

    second = asyncio.run(multi.fetch_next_async())
    assert second is not None and second.source_id == "source-1"
    assert source2.calls == 0

    third = asyncio.run(multi.fetch_next_async())
    assert third is not None and third.source_id == "source-2"
    assert source1.is_historical_complete()
    assert source2.calls == 1


def test_multisource_recovery_blocks_later_source():
    source1 = _FakeSource("source-1", [])
    source1._failed = True
    source2 = _FakeSource("source-2", ["201"])
    db = _FakeDB()

    multi = MultiTelegramSource.__new__(MultiTelegramSource)
    multi.db = db
    multi.sources = (source1, source2)
    multi._cursor = 0
    multi._last_source = None
    multi._pending_source = None
    multi._pending_message = None
    multi._pending_checkpoints = {}

    result = asyncio.run(multi.fetch_next_async())
    assert result is None
    assert source2.calls == 0


def test_vision_non_product_url_is_waiting_vision(monkeypatch):
    def unresolved(_url):
        raise ValueError("Não foi possível extrair shop_id/item_id")

    monkeypatch.setattr("ArmoredVision.service.resolve_short_url", unresolved)

    item = SimpleNamespace(original_url="https://creator.shopee.com.br/insight/live")
    with __import__("pytest").raises(VisionUnresolvedError):
        ArmoredVision().identify(item)
