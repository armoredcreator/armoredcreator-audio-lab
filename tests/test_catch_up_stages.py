import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from armored_core.catch_up_stages import run_catch_up_stage
from armored_core.models import State


class FakeSource:
    def __init__(self, messages=()):
        self.sources = tuple(
            SimpleNamespace(source_id=source_id)
            for source_id in ("source-1", "source-2", "source-3")
        )
        self.messages = list(messages)
        self.historical_scan_exhausted = True
        self.historical_limit_reached = False
        self.historical_collection_limited = False
        self.marked = []
        self.downloads = []

    async def iter_historical_candidates_async(self):
        for message in self.messages:
            yield message

    def mark_ingested(self, message_id):
        self.marked.append(str(message_id))

    async def materialize_candidate_async(self, source_id, message_id, target):
        self.downloads.append((str(source_id), str(message_id)))
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_bytes(b"APPROVED-ORIGINAL")


class FakeDB:
    def __init__(self, items=()):
        self.items = {item.content_id: item for item in items}
        self.conn = SimpleNamespace(
            execute=lambda _sql, _params=(): SimpleNamespace(fetchone=lambda: (0,))
        )

    def pending_vision_candidates(self):
        return [item for item in self.items.values() if not item.affiliate_url]

    def pending_vision_approved_items(self):
        return [item for item in self.items.values() if item.affiliate_url]

    def get(self, item_id):
        return self.items[item_id]

    def historical_complete(self):
        return False


class FakeSync:
    def __init__(self, db):
        self.db = db
        self.reserved = []

    def reserve_message(self, message):
        item_id = f"{message.source_id}:{message.telegram_message_id}"
        self.reserved.append(item_id)
        if item_id not in self.db.items:
            path = Path(tempfile.gettempdir()) / "catch-up-test" / item_id.replace(":", "_") / "original.mp4"
            self.db.items[item_id] = SimpleNamespace(
                content_id=item_id,
                telegram_message_id=str(message.telegram_message_id),
                source_id=str(message.source_id),
                topic_id=getattr(message, "topic_id", None),
                topic_name=getattr(message, "topic_name", None),
                original_url=getattr(message, "original_url", None),
                affiliate_url=None,
                state=State.RECEIVED,
                original_path=path,
            )
        return item_id

    async def materialize_message_async(self, message):
        item_id = f"{message.source_id}:{message.telegram_message_id}"
        item = self.db.items[item_id]
        item.original_path.parent.mkdir(parents=True, exist_ok=True)
        await message.materialize(item.original_path)


class FakePipeline:
    def __init__(self, db):
        self.db = db
        self.calls = []

    def run(self, item_id, *, stop_after_vision=False):
        self.calls.append((item_id, stop_after_vision))
        item = self.db.get(item_id)
        item.affiliate_url = "https://example.invalid/approved"
        item.state = State.RECEIVED


class FakeCoordinator:
    def __init__(self, source, db):
        self.source = source
        self.db = db
        self.sync = FakeSync(db)
        self.pipeline = FakePipeline(db)
        self.production_calls = 0

    async def _release_source_connection(self):
        return None

    async def _ensure_source_connection(self):
        return None

    def run(self, *_args, **_kwargs):
        self.production_calls += 1


class CatchUpStageTests(unittest.TestCase):
    def test_sync_only_reserves_all_sources_and_never_downloads_or_runs_pipeline(self):
        messages = [
            SimpleNamespace(
                source_id=f"source-{index}",
                telegram_message_id=str(100 + index),
                topic_id=index,
                topic_name=f"topic-{index}",
                original_url=f"https://example.invalid/{index}",
                materialize=lambda _target: self.fail("sync stage must not download"),
            )
            for index in (1, 2, 3)
        ]
        source = FakeSource(messages)
        db = FakeDB()
        coordinator = FakeCoordinator(source, db)

        report = asyncio.run(run_catch_up_stage(coordinator, "sync"))

        self.assertEqual(report["processed"], 3)
        self.assertEqual([x.split(":")[0] for x in coordinator.sync.reserved],
                         ["source-1", "source-2", "source-3"])
        self.assertEqual(source.downloads, [])
        self.assertEqual(coordinator.pipeline.calls, [])
        self.assertEqual(coordinator.production_calls, [])
        self.assertFalse(report["historical_complete"])

    def test_vision_only_calls_vision_gate_and_does_not_download_or_produce(self):
        item = SimpleNamespace(
            content_id="source-1:101",
            telegram_message_id="101",
            source_id="source-1",
            topic_id=1,
            topic_name="topic",
            original_url="https://example.invalid/item",
            affiliate_url=None,
            state=State.RECEIVED,
            original_path=Path(tempfile.gettempdir()) / "does-not-exist-catch-up.mp4",
        )
        source = FakeSource()
        db = FakeDB([item])
        coordinator = FakeCoordinator(source, db)

        report = asyncio.run(run_catch_up_stage(coordinator, "vision"))

        self.assertEqual(coordinator.pipeline.calls, [("source-1:101", True)])
        self.assertEqual(source.downloads, [])
        self.assertEqual(coordinator.production_calls, 0)
        self.assertEqual(report["processed"], 1)

    def test_stock_downloads_only_approved_original_and_never_runs_production(self):
        with tempfile.TemporaryDirectory() as td:
            item = SimpleNamespace(
                content_id="source-2:202",
                telegram_message_id="202",
                source_id="source-2",
                topic_id=2,
                topic_name="topic",
                original_url="https://example.invalid/item",
                affiliate_url="https://example.invalid/approved",
                state=State.RECEIVED,
                original_path=Path(td) / "source-2" / "202.mp4",
            )
            source = FakeSource()
            db = FakeDB([item])
            coordinator = FakeCoordinator(source, db)

            report = asyncio.run(run_catch_up_stage(coordinator, "stock"))

            self.assertEqual(source.downloads, [("source-2", "202")])
            self.assertEqual(item.original_path.read_bytes(), b"APPROVED-ORIGINAL")
            self.assertEqual(coordinator.pipeline.calls, [])
            self.assertEqual(coordinator.production_calls, 0)
            self.assertEqual(report["processed"], 1)


if __name__ == "__main__":
    unittest.main()
