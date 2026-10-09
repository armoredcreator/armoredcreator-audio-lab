import asyncio
from pathlib import Path
from types import SimpleNamespace

from armored_core.coordinator import Coordinator
from armored_core.database import Database
from armored_core.models import State
from armored_core.services import VisionResult, VisionUnresolvedError
from armored_core.storage import Storage
from armored_core.catch_up_stages import run_catch_up_stage


SOURCE_IDS = (
    "-1003788989075",
    "-1002698134896",
    "-1002039708059",
)


class FakeVision:
    def identify(self, item):
        if not item.original_url:
            raise VisionUnresolvedError("link Shopee ausente")
        return VisionResult(
            "produto-validado",
            f"https://affiliate.invalid/{item.telegram_message_id}",
            affiliate_urls=(f"https://affiliate.invalid/{item.telegram_message_id}",),
            ia_context={"productName": "produto-validado"},
        )


class ForbiddenStudio:
    calls = 0

    def process(self, item):
        self.calls += 1
        raise AssertionError("Studio não pode executar nas três etapas iniciais")


class ForbiddenPublisher:
    calls = 0

    def publish(self, item):
        self.calls += 1
        raise AssertionError("Hub/publicação não pode executar nas três etapas iniciais")


class FakeMultiTelegramSource:
    def __init__(self, messages):
        self.sources = tuple(SimpleNamespace(source_id=value) for value in SOURCE_IDS)
        self.messages = list(messages)
        self.downloads = []
        self.historical_scan_exhausted = False
        self.historical_limit_reached = False
        self.historical_collection_limited = False
        self.historical_materialization_failed = False

    def is_historical_complete(self):
        return False

    async def iter_historical_candidates_async(self):
        for message in self.messages:
            yield message
        self.historical_scan_exhausted = True

    def mark_ingested(self, _message_id):
        pass

    def mark_materialization_failed(self):
        self.historical_materialization_failed = True

    async def materialize_candidate_async(self, source_id, message_id, target):
        self.downloads.append((str(source_id), str(message_id)))
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_bytes(f"ORIGINAL:{source_id}:{message_id}".encode())


def _messages():
    entries = (
        (SOURCE_IDS[0], "1001", "https://shopee.com.br/product/1001"),
        (SOURCE_IDS[0], "1002", None),
        (SOURCE_IDS[1], "1003", "https://shopee.com.br/product/1003"),
        (SOURCE_IDS[2], "1004", "https://shopee.com.br/product/1004"),
    )
    result = []
    for index, (source_id, message_id, original_url) in enumerate(entries, start=1):
        result.append(
            SimpleNamespace(
                telegram_message_id=message_id,
                source_id=source_id,
                topic_id=index * 10,
                topic_name=f"topic-{index}",
                original_url=original_url,
                source_path=None,
                materialize=lambda _target: (_ for _ in ()).throw(
                    AssertionError("Sync/Vision não podem baixar mídia")
                ),
            )
        )
    return result

def _coordinator(root, messages, studio, publisher):
    storage = Storage(root)
    db = Database(storage.database / "armoredcreator.db")
    source = FakeMultiTelegramSource(messages)
    return Coordinator(db, storage, FakeVision(), studio, publisher, source=source), source


def test_sync_vision_stock_are_durable_separate_stages_end_to_end(tmp_path, monkeypatch):
    for index, source_id in enumerate(SOURCE_IDS, start=1):
        monkeypatch.setenv(f"ARMORED_SOURCE_{index}_ID", source_id)

    messages = _messages()
    studio = ForbiddenStudio()
    publisher = ForbiddenPublisher()

    # Stage 1: reserve every source in SQLite, without downloading any bytes.
    sync_coordinator, sync_source = _coordinator(tmp_path, messages, studio, publisher)
    try:
        sync_report = asyncio.run(run_catch_up_stage(sync_coordinator, "sync"))
        assert sync_report["processed"] == 4
        assert sync_report["historical_scan_exhausted"] is True
        assert sync_report["historical_complete"] is False
        assert sync_source.downloads == []
        assert all(
            not sync_coordinator.db.get(f"{source_id}_{message_id}").original_path.is_file()
            for source_id, message_id, _url in (
                (SOURCE_IDS[0], "1001", "url"),
                (SOURCE_IDS[0], "1002", None),
                (SOURCE_IDS[1], "1003", "url"),
                (SOURCE_IDS[2], "1004", "url"),
            )
        )
    finally:
        sync_coordinator.close()

    # Stage 2: Vision persists approvals; no original, Studio or publication.
    vision_coordinator, vision_source = _coordinator(tmp_path, messages, studio, publisher)
    try:
        vision_report = asyncio.run(run_catch_up_stage(vision_coordinator, "vision"))
        assert vision_report["processed"] == 4
        assert vision_report["outcomes"]["approved_this_stage"] == 3
        assert vision_report["outcomes"]["waiting_vision_this_stage"] == 1
        assert vision_report["outcomes"]["approved_total"] == 3
        assert vision_report["outcomes"]["waiting_vision_total"] == 1
        assert vision_report["errors"] == []
        assert vision_source.downloads == []
        expected_vision = (
            (SOURCE_IDS[0], "1001", State.RECEIVED, True),
            (SOURCE_IDS[0], "1002", State.WAITING_VISION, False),
            (SOURCE_IDS[1], "1003", State.RECEIVED, True),
            (SOURCE_IDS[2], "1004", State.RECEIVED, True),
        )
        for source_id, message_id, expected_state, has_affiliate in expected_vision:
            item = vision_coordinator.db.get(f"{source_id}_{message_id}")
            assert item.state == expected_state
            assert bool(item.affiliate_url) is has_affiliate
            assert not item.original_path.is_file()
    finally:
        vision_coordinator.close()

    # Stage 3: only approved originals are materialized, sequentially by source.
    stock_coordinator, stock_source = _coordinator(tmp_path, messages, studio, publisher)
    try:
        stock_report = asyncio.run(run_catch_up_stage(stock_coordinator, "stock"))
        assert stock_report["processed"] == 3
        assert stock_report["outcomes"]["downloaded"] == 3
        assert stock_report["errors"] == []
        assert stock_source.downloads == [
            (SOURCE_IDS[0], "1001"),
            (SOURCE_IDS[1], "1003"),
            (SOURCE_IDS[2], "1004"),
        ]
        for index, (source_id, message_id) in enumerate(
            ((SOURCE_IDS[0], "1001"), (SOURCE_IDS[1], "1003"), (SOURCE_IDS[2], "1004")),
            start=1,
        ):
            item = stock_coordinator.db.get(f"{source_id}_{message_id}")
            assert item.state == State.RECEIVED
            assert item.original_path.is_file()
            assert item.original_path.parent == (
                tmp_path / "storage" / f"Videos GRUPO_FONTE_{index}" / message_id
            )
            assert item.original_path.name == f"{message_id}_finallinkoriginal.mp4"
        assert not stock_coordinator.db.historical_complete()
        assert studio.calls == 0
        assert publisher.calls == 0
    finally:
        stock_coordinator.close()
