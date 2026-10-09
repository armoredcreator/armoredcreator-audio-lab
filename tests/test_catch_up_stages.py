import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from armored_core.catch_up_stages import run_catch_up_stage
from armored_core.models import State


SOURCE_IDS = ("source-1", "source-2", "source-3")


class FakeSource:
    def __init__(self, messages=()):
        self.sources = tuple(SimpleNamespace(source_id=value) for value in SOURCE_IDS)
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

    def pre_download_recovery_items(self):
        return [
            item for item in self.items.values()
            if item.state == State.RECOVERY and not item.original_path.is_file()
        ]

    def vision_approved_interrupted_items(self):
        return [
            item for item in self.items.values()
            if item.state == State.VISION and item.affiliate_url
        ]

    def transition(self, item_id, new_state, _reason=""):
        self.items[item_id].state = new_state

    def pending_vision_candidates(self):
        return [
            item for item in self.items.values()
            if not item.affiliate_url and item.state in (State.RECEIVED, State.VISION)
        ]

    def pending_vision_approved_items(self):
        return [item for item in self.items.values() if item.affiliate_url]

    def get(self, item_id):
        return self.items[item_id]

    def repair_original_path(self, item_id, path):
        self.items[item_id].original_path = Path(path)

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
        self.storage = SimpleNamespace(
            original=lambda content_id, suffix, original_url=None, source_id=None:
                self.db.get(content_id).original_path
        )
        self.production_calls = 0

    async def _release_source_connection(self):
        return None

    async def _ensure_source_connection(self):
        return None

    def run(self, *_args, **_kwargs):
        self.production_calls += 1


def make_item(source_id, message_id, path):
    return SimpleNamespace(
        content_id=f"{source_id}:{message_id}",
        telegram_message_id=str(message_id),
        source_id=source_id,
        topic_id=SOURCE_IDS.index(source_id) + 1,
        topic_name=f"topic-{source_id}",
        original_url=f"https://example.invalid/{message_id}",
        affiliate_url=None,
        state=State.RECEIVED,
        original_path=Path(path),
    )


class CatchUpStageTests(unittest.TestCase):
    def test_sync_refuses_finite_historical_limit_before_reserving_anything(self):
        source = FakeSource()
        source.historical_collection_limited = True
        db = FakeDB([])
        coordinator = FakeCoordinator(source, db)

        with self.assertRaisesRegex(RuntimeError, "ARMORED_SYNC_CATCHUP_LIMIT"):
            asyncio.run(run_catch_up_stage(coordinator, "sync"))

        self.assertEqual(db.items, {})
        self.assertEqual(source.downloads, [])

    def test_sync_reports_partial_counts_when_telegram_discovery_fails(self):
        message = SimpleNamespace(
            source_id=SOURCE_IDS[0],
            telegram_message_id="101",
            topic_id=1,
            topic_name="topic-source-1",
            original_url="https://example.invalid/101",
            materialize=lambda _target: self.fail("sync stage must not download"),
        )

        class FailingDiscoverySource(FakeSource):
            async def iter_historical_candidates_async(self):
                yield message
                raise RuntimeError("telegram-source-disconnected")

        source = FailingDiscoverySource([message])
        source.historical_scan_exhausted = False
        coordinator = FakeCoordinator(source, FakeDB())

        report = asyncio.run(run_catch_up_stage(coordinator, "sync"))

        self.assertEqual(report["processed"], 1)
        self.assertEqual(report["per_source"][SOURCE_IDS[0]], 1)
        self.assertTrue(any("telegram-source-disconnected" in error for error in report["errors"]))
        self.assertFalse(report["historical_scan_exhausted"])
        self.assertFalse(report["historical_complete"])
        self.assertEqual(source.downloads, [])

    def test_sync_stops_at_first_reservation_error(self):
        messages = [
            SimpleNamespace(
                source_id=source_id,
                telegram_message_id=str(100 + index),
                topic_id=index,
                topic_name=f"topic-{source_id}",
                original_url=f"https://example.invalid/{index}",
                materialize=lambda _target: self.fail("sync stage must not download"),
            )
            for index, source_id in enumerate(SOURCE_IDS, start=1)
        ]
        source = FakeSource(messages)
        source.historical_scan_exhausted = False
        db = FakeDB()
        coordinator = FakeCoordinator(source, db)
        original_reserve = coordinator.sync.reserve_message

        def fail_second(message):
            if message.telegram_message_id == "102":
                raise RuntimeError("sqlite-reservation-failed")
            return original_reserve(message)

        coordinator.sync.reserve_message = fail_second
        report = asyncio.run(run_catch_up_stage(coordinator, "sync"))

        self.assertEqual(report["processed"], 1)
        self.assertEqual(len(report["errors"]), 2)  # write failure + incomplete scan
        self.assertIn("sqlite-reservation-failed", report["errors"][0])
        self.assertFalse(report["historical_scan_exhausted"])
        self.assertEqual(
            [message.telegram_message_id for message in messages[:2]],
            ["101", "102"],
        )
        self.assertEqual(len(coordinator.sync.reserved), 1)
        self.assertEqual(source.downloads, [])

    def test_sync_only_reserves_all_sources_and_never_downloads_or_runs_pipeline(self):
        messages = [
            SimpleNamespace(
                source_id=source_id,
                telegram_message_id=str(100 + index),
                topic_id=index,
                topic_name=f"topic-{source_id}",
                original_url=f"https://example.invalid/{index}",
                materialize=lambda _target: self.fail("sync stage must not download"),
            )
            for index, source_id in enumerate(SOURCE_IDS, start=1)
        ]
        source = FakeSource(messages)
        db = FakeDB()
        coordinator = FakeCoordinator(source, db)

        report = asyncio.run(run_catch_up_stage(coordinator, "sync"))

        self.assertEqual(report["processed"], 3)
        self.assertEqual([x.split(":")[0] for x in coordinator.sync.reserved], list(SOURCE_IDS))
        self.assertEqual(source.downloads, [])
        self.assertEqual(coordinator.pipeline.calls, [])
        self.assertEqual(coordinator.production_calls, 0)
        self.assertFalse(report["historical_complete"])

    def test_vision_runs_in_configured_source_order_without_download_or_production(self):
        items = [
            make_item(source_id, 200 + index, Path(tempfile.gettempdir()) / f"vision-{source_id}.mp4")
            for index, source_id in enumerate(SOURCE_IDS, start=1)
        ]
        source = FakeSource()
        db = FakeDB(items)
        coordinator = FakeCoordinator(source, db)

        report = asyncio.run(run_catch_up_stage(coordinator, "vision"))

        self.assertEqual(
            coordinator.pipeline.calls,
            [(item.content_id, True) for item in items],
        )
        self.assertEqual(source.downloads, [])
        self.assertEqual(coordinator.production_calls, 0)
        self.assertEqual(report["processed"], 3)

    def test_vision_retries_legacy_recovery_without_original(self):
        item = make_item(
            SOURCE_IDS[0],
            198,
            Path(tempfile.gettempdir()) / "vision-legacy-recovery.mp4",
        )
        item.state = State.RECOVERY
        source = FakeSource()
        db = FakeDB([item])
        coordinator = FakeCoordinator(source, db)

        report = asyncio.run(run_catch_up_stage(coordinator, "vision"))

        self.assertEqual(item.state, State.RECEIVED)
        self.assertEqual(coordinator.pipeline.calls, [(item.content_id, True)])
        self.assertEqual(report["outcomes"]["recovered_pre_download_recovery"], 1)
        self.assertEqual(report["errors"], [])

    def test_vision_recovers_approved_evidence_after_crash_before_state_transition(self):
        item = make_item(
            SOURCE_IDS[0],
            199,
            Path(tempfile.gettempdir()) / "vision-crash-window.mp4",
        )
        item.state = State.VISION
        item.affiliate_url = "https://example.invalid/approved"
        source = FakeSource()
        db = FakeDB([item])
        coordinator = FakeCoordinator(source, db)

        report = asyncio.run(run_catch_up_stage(coordinator, "vision"))

        self.assertEqual(item.state, State.RECEIVED)
        self.assertEqual(coordinator.pipeline.calls, [])
        self.assertEqual(report["processed"], 0)
        self.assertEqual(report["errors"], [])

    def test_vision_stops_on_first_technical_failure_to_preserve_order(self):
        items = [
            make_item(source_id, 250 + index, Path(tempfile.gettempdir()) / f"vision-fail-{source_id}.mp4")
            for index, source_id in enumerate(SOURCE_IDS, start=1)
        ]
        source = FakeSource()
        db = FakeDB(items)
        coordinator = FakeCoordinator(source, db)

        class FailingPipeline:
            def __init__(self):
                self.calls = []

            def run(self, item_id, *, stop_after_vision=False):
                self.calls.append((item_id, stop_after_vision))
                if item_id == items[1].content_id:
                    raise RuntimeError("vision-provider-timeout")
                item = db.get(item_id)
                item.affiliate_url = "https://example.invalid/approved"
                item.state = State.RECEIVED

        coordinator.pipeline = FailingPipeline()
        report = asyncio.run(run_catch_up_stage(coordinator, "vision"))

        self.assertEqual(
            coordinator.pipeline.calls,
            [(items[0].content_id, True), (items[1].content_id, True)],
        )
        self.assertEqual(report["processed"], 1)
        self.assertEqual(len(report["errors"]), 1)
        self.assertFalse(items[2].affiliate_url)

    def test_stock_refuses_to_download_while_vision_candidates_remain(self):
        item = make_item(
            SOURCE_IDS[0],
            349,
            Path(tempfile.gettempdir()) / "stock-before-vision.mp4",
        )
        source = FakeSource()
        db = FakeDB([item])
        coordinator = FakeCoordinator(source, db)

        with self.assertRaisesRegex(RuntimeError, "ArmoredStock bloqueado"):
            asyncio.run(run_catch_up_stage(coordinator, "stock"))

        self.assertEqual(source.downloads, [])
        self.assertFalse(item.original_path.is_file())

    def test_stock_stops_on_first_download_failure_to_preserve_order(self):
        with tempfile.TemporaryDirectory() as td:
            items = [
                make_item(source_id, 350 + index, Path(td) / source_id / f"{350 + index}.mp4")
                for index, source_id in enumerate(SOURCE_IDS, start=1)
            ]
            for item in items:
                item.affiliate_url = "https://example.invalid/approved"

            class FailingSource(FakeSource):
                def __init__(self):
                    super().__init__()
                    self.attempts = []

                async def materialize_candidate_async(self, source_id, message_id, target):
                    self.attempts.append((str(source_id), str(message_id)))
                    if str(source_id) == SOURCE_IDS[1]:
                        raise RuntimeError("telegram-download-failed")
                    await super().materialize_candidate_async(source_id, message_id, target)

            source = FailingSource()
            db = FakeDB(items)
            coordinator = FakeCoordinator(source, db)

            report = asyncio.run(run_catch_up_stage(coordinator, "stock"))

            self.assertEqual(
                source.attempts,
                [(SOURCE_IDS[0], "351"), (SOURCE_IDS[1], "352")],
            )
            self.assertEqual(report["processed"], 1)
            self.assertEqual(len(report["errors"]), 1)
            self.assertFalse(items[2].original_path.is_file())
            self.assertEqual(coordinator.production_calls, 0)

    def test_stock_downloads_only_approved_items_in_source_order_and_never_produces(self):
        with tempfile.TemporaryDirectory() as td:
            items = [
                make_item(source_id, 300 + index, Path(td) / source_id / f"{300 + index}.mp4")
                for index, source_id in enumerate(SOURCE_IDS, start=1)
            ]
            for item in items:
                item.affiliate_url = "https://example.invalid/approved"
            source = FakeSource()
            db = FakeDB(items)
            coordinator = FakeCoordinator(source, db)

            report = asyncio.run(run_catch_up_stage(coordinator, "stock"))

            self.assertEqual(
                source.downloads,
                [(item.source_id, item.telegram_message_id) for item in items],
            )
            self.assertTrue(all(item.original_path.read_bytes() == b"APPROVED-ORIGINAL" for item in items))
            self.assertEqual(coordinator.pipeline.calls, [])
            self.assertEqual(coordinator.production_calls, 0)
            self.assertEqual(report["processed"], 3)


if __name__ == "__main__":
    unittest.main()
