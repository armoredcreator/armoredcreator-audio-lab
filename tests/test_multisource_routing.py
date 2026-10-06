from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from armored_core.database import Database
from armored_core.routing import load_routes
from ArmoredHub.service import ArmoredHub


def test_load_routes_supports_source1_source2_and_hub1_hub2(monkeypatch):
    monkeypatch.setenv("ARMORED_SOURCE_1_KEY", "source1")
    monkeypatch.setenv("ARMORED_SOURCE_1_CHAT_ID", "-1003788989075")
    monkeypatch.setenv("ARMORED_SOURCE_1_ID", "-1003788989075")
    monkeypatch.setenv("ARMORED_SOURCE_1_HUB", "hub1")
    monkeypatch.setenv("ARMORED_SOURCE_2_KEY", "source2")
    monkeypatch.setenv("ARMORED_SOURCE_2_CHAT_ID", "-1002698134896")
    monkeypatch.setenv("ARMORED_SOURCE_2_ID", "-1002698134896")
    monkeypatch.setenv("ARMORED_SOURCE_2_HUB", "hub2")
    monkeypatch.setenv("ARMORED_HUB_HUB1_CHAT_ID", "-1004341972306")
    monkeypatch.setenv("ARMORED_HUB_HUB1_TOPIC_ID", "228")
    monkeypatch.setenv("ARMORED_HUB_HUB2_CHAT_ID", "-1004341972306")
    monkeypatch.setenv("ARMORED_HUB_HUB2_TOPIC_ID", "1160")

    routes = load_routes()

    assert len(routes) == 2
    assert routes[0].source.source_id == "-1003788989075"
    assert routes[0].hub.topic_id == 228
    assert routes[1].source.source_id == "-1002698134896"
    assert routes[1].hub.topic_id == 1160


def test_same_telegram_message_id_can_exist_in_two_sources(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")

    first = db.reserve_item("77", source_id="-1003788989075", original_path=tmp_path / "one.mp4")
    second = db.reserve_item("77", source_id="-1002698134896", original_path=tmp_path / "two.mp4")

    assert first != second
    assert db.get(first).source_id == "-1003788989075"
    assert db.get(second).source_id == "-1002698134896"


def test_source_checkpoints_are_independent(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")

    db.set_source_sync_topic_checkpoint("-1003788989075", 228, "one", 100)
    db.set_source_sync_topic_checkpoint("-1002698134896", 228, "two", 200)

    assert db.source_sync_topic_checkpoint("-1003788989075", 228) == 100
    assert db.source_sync_topic_checkpoint("-1002698134896", 228) == 200


def test_hub_routes_source2_to_hub2(tmp_path: Path):
    route = SimpleNamespace(
        source=SimpleNamespace(source_id="-1002698134896"),
        hub=SimpleNamespace(chat_id="-1004341972306", topic_id=1160),
    )
    item = SimpleNamespace(
        content_id="-1002698134896:77",
        item_id="-1002698134896:77",
        source_id="-1002698134896",
    )
    hub = ArmoredHub(tmp_path, Database(tmp_path / "db.sqlite"), routes=(route,))

    assert hub._destination_for(item) == ("-1004341972306", 1160)
