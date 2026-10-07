from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ArmoredHub.service import ArmoredHub
from ArmoredSync.service import MultiTelegramSource, TelegramSource
from armored_core.database import Database
from armored_core.routing import load_routes
from armored_core.storage import Storage
from armored_core.models import State


def _routes(monkeypatch):
    monkeypatch.setenv("ARMORED_SOURCE_1_KEY", "source1")
    monkeypatch.setenv("ARMORED_SOURCE_1_CHAT_ID", "-1003788989075")
    monkeypatch.setenv("ARMORED_SOURCE_1_ID", "-1003788989075")
    monkeypatch.setenv("ARMORED_SOURCE_2_KEY", "source2")
    monkeypatch.setenv("ARMORED_SOURCE_2_CHAT_ID", "-1002698134896")
    monkeypatch.setenv("ARMORED_SOURCE_2_ID", "-1002698134896")
    monkeypatch.setenv("ARMORED_HUB_1_CHAT_ID", "-1004341972306")
    monkeypatch.setenv("ARMORED_HUB_1_TOPIC_ID", "228")
    monkeypatch.setenv("ARMORED_HUB_2_CHAT_ID", "-1004341972306")
    monkeypatch.setenv("ARMORED_HUB_2_TOPIC_ID", "1160")
    return load_routes()


def test_routes_are_explicit_and_do_not_share_hub_topics(monkeypatch):
    routes = _routes(monkeypatch)
    assert [(r.source.source_id, r.hub.topic_id) for r in routes] == [
        ("-1003788989075", 228),
        ("-1002698134896", 1160),
    ]


def test_same_telegram_message_id_is_source_scoped_and_windows_safe(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")

    first = db.reserve_item("77", source_id="-1003788989075", original_path=tmp_path / "one.mp4")
    second = db.reserve_item("77", source_id="-1002698134896", original_path=tmp_path / "two.mp4")

    assert first != second
    assert ":" not in second
    assert db.get(first).source_id == "-1003788989075"
    assert db.get(second).source_id == "-1002698134896"


def test_source_checkpoints_are_independent(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")

    db.set_source_sync_topic_checkpoint("-1003788989075", 228, "hub1", 100)
    db.set_source_sync_topic_checkpoint("-1002698134896", 228, "hub2", 200)

    assert db.source_sync_topic_checkpoint("-1003788989075", 228) == 100
    assert db.source_sync_topic_checkpoint("-1002698134896", 228) == 200


def test_shopee_url_deduplication_is_source_scoped(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")
    url = "https://shopee.com.br/product/1"

    db.reserve_item("10", source_id="-1003788989075", original_url=url, original_path=tmp_path / "a.mp4")
    source2 = TelegramSource(
        tmp_path,
        SimpleNamespace(),
        db,
        source="-1002698134896",
        source_id="-1002698134896",
    )
    source1 = TelegramSource(
        tmp_path,
        SimpleNamespace(),
        db,
        source="-1003788989075",
        source_id="-1003788989075",
    )

    assert source1._shopee_url_exists(url) is True
    assert source2._shopee_url_exists(url) is False


def test_hub_routes_source2_to_hub2(tmp_path: Path, monkeypatch):
    routes = (
        SimpleNamespace(
            source=SimpleNamespace(source_id="-1003788989075"),
            hub=SimpleNamespace(chat_id="-1004341972306", topic_id=228),
        ),
        SimpleNamespace(
            source=SimpleNamespace(source_id="-1002698134896"),
            hub=SimpleNamespace(chat_id="-1004341972306", topic_id=1160),
        ),
    )
    monkeypatch.delenv("ARMORED_HUB_TOPIC_ID", raising=False)
    monkeypatch.delenv("ARMORED_CREATOR_GROUP_ID", raising=False)
    item = SimpleNamespace(
        content_id="-1002698134896_77",
        item_id="-1002698134896_77",
        source_id="-1002698134896",
    )
    hub = ArmoredHub(tmp_path, Database(tmp_path / "db.sqlite"), routes=routes)
    assert hub._destination_for(item) == ("-1004341972306", 1160)


def test_multi_source_keeps_one_candidate_pending():
    class FakeSource:
        def __init__(self, source_id, message_id):
            self.source_id = source_id
            self.message_id = message_id
            self.calls = 0
            self.committed = 0
            self.historical_materialization_failed = False
            self.historical_scan_exhausted = False
            self.historical_limit_reached = False
            self.historical_collection_limited = False

        def is_historical_complete(self):
            return True

        async def fetch_live_candidate_async(self):
            self.calls += 1
            return SimpleNamespace(
                telegram_message_id=str(self.message_id),
                source_id=self.source_id,
                topic_id=1,
            ), {1: self.message_id}

        def commit_live_checkpoints(self, checkpoints):
            self.committed += 1

        def reset_historical_scan(self):
            pass

        def mark_ingested(self, _):
            pass

        def mark_materialization_failed(self):
            pass

    routes = (
        SimpleNamespace(source=SimpleNamespace(chat_id="s1", source_id="s1")),
        SimpleNamespace(source=SimpleNamespace(chat_id="s2", source_id="s2")),
    )
    wrapper = MultiTelegramSource.__new__(MultiTelegramSource)
    wrapper.db = SimpleNamespace(all_sources_historical_complete=lambda ids: True)
    wrapper.routes = routes
    wrapper.sources = (FakeSource("s1", 10), FakeSource("s2", 20))
    wrapper._cursor = 0
    wrapper._last_source = None
    wrapper._pending_source = None
    wrapper._pending_message = None
    wrapper._pending_checkpoints = {}

    import asyncio
    first, first_cp = asyncio.run(wrapper.fetch_live_candidate_async())
    second, second_cp = asyncio.run(wrapper.fetch_live_candidate_async())

    assert first.telegram_message_id == "10"
    assert second.telegram_message_id == "10"
    assert first_cp == second_cp
    assert wrapper.sources[0].calls == 1
    assert wrapper.sources[1].calls == 0

    wrapper.commit_live_checkpoints(first_cp)
    third, _ = asyncio.run(wrapper.fetch_live_candidate_async())
    assert third.telegram_message_id == "20"

def test_legacy_source1_state_is_migrated_without_making_source2_live(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")
    db.set_sync_mode("LIVE")
    db.set_sync_topic_checkpoint(228, "hub1", 900)

    db.initialize_source_state("-1003788989075", legacy_source_id="-1003788989075")
    db.initialize_source_state("-1002698134896", legacy_source_id="-1003788989075")

    assert db.source_sync_mode("-1003788989075") == "LIVE"
    assert db.source_sync_mode("-1002698134896") == "CATCH_UP"
    assert db.source_sync_topic_checkpoint("-1003788989075", 228) == 900
    assert db.source_sync_topic_checkpoint("-1002698134896", 228) == 0


def test_source_workspaces_are_physically_separated(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ARMORED_SOURCE_1_ID", "-1003788989075")
    monkeypatch.setenv("ARMORED_SOURCE_2_ID", "-1002698134896")

    storage = Storage(tmp_path)
    first = storage.workspace("77", source_id="-1003788989075")
    second = storage.workspace("77", source_id="-1002698134896")

    assert first != second
    assert first == tmp_path / "storage" / "Videos GRUPO_FONTE_1" / "77"
    assert second == tmp_path / "storage" / "Videos GRUPO_FONTE_2" / "77"
    assert storage.original(
        "77",
        original_url="https://shopee.com.br/product/1",
        source_id="-1003788989075",
    ).parent == first
    assert storage.result(
        "77",
        affiliate_url="https://shopee.com.br/product/1",
        source_id="-1002698134896",
    ).parent == second


def test_recovery_item_is_not_hidden_by_source_scoped_url_dedup(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")
    url = "https://shopee.com.br/product/recover-me"
    source = "-1003788989075"
    recovery_id = db.reserve_item(
        "99",
        source_id=source,
        original_url=url,
        original_path=tmp_path / "missing.mp4",
    )
    db.set_vision(recovery_id, "produto", "https://shopee.com.br/product/affiliate")
    db.transition(recovery_id, State.RECOVERY, "download-timeout")

    telegram = TelegramSource(
        tmp_path,
        SimpleNamespace(),
        db,
        source=source,
        source_id=source,
    )

    assert telegram._shopee_url_exists(url) is False


def test_completed_item_still_suppresses_source_scoped_url_dedup(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")
    url = "https://shopee.com.br/product/completed"
    source = "-1003788989075"
    completed_id = db.reserve_item(
        "100",
        source_id=source,
        original_url=url,
        original_path=tmp_path / "missing.mp4",
    )
    db.transition(completed_id, State.PUBLISHED, "publication-confirmed")
    db.mark_cleanup_completed(completed_id)

    telegram = TelegramSource(
        tmp_path,
        SimpleNamespace(),
        db,
        source=source,
        source_id=source,
    )

    assert telegram._shopee_url_exists(url) is True
