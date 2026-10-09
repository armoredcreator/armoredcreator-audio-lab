import asyncio
import tempfile
import unittest
from pathlib import Path

from armored_core.coordinator import Coordinator
from armored_core.database import Database
from armored_core.models import PublicationCheck, State
from armored_core.services import (
    IngestMessage,
    PublicationResult,
    StudioResult,
    VisionResult,
    VisionUnresolvedError,
)
from armored_core.storage import Storage


class _VisionResolved:
    def identify(self, item):
        return VisionResult(
            "Produto",
            "https://affiliate.invalid/product",
            affiliate_urls=("https://affiliate.invalid/product",),
            ia_context={"productName": "Produto"},
        )


class _VisionUnresolved:
    def identify(self, item):
        raise VisionUnresolvedError("produto não resolvido")


class _VisionTechnicalFailure:
    def identify(self, item):
        raise RuntimeError("vision-provider-timeout")


class _Studio:
    def __init__(self, storage):
        self.storage = storage
        self.calls = 0

    def process(self, item):
        self.calls += 1
        working = self.storage.working(item.content_id)
        result = self.storage.result(
            item.content_id,
            item.affiliate_url,
            item.affiliate_name,
        )
        working.write_bytes(item.original_path.read_bytes())
        result.write_bytes(b"processed")
        return StudioResult(working, result)


class _Publisher:
    def __init__(self):
        self.calls = 0

    def check_publication(self, item):
        return PublicationCheck.ABSENT

    def publish(self, item):
        self.calls += 1
        return PublicationResult(True, f"telegram-{self.calls}")


class _GateSource:
    def __init__(self, vision, materializer):
        self.vision = vision
        self.materializer = materializer


class VisionBeforeDownloadTests(unittest.TestCase):
    def _message(self, item_id, materializer):
        return IngestMessage(
            telegram_message_id=item_id,
            source_id="telegram",
            topic_id=1,
            topic_name="topic",
            original_url="https://shopee.com.br/product/123",
            materialize=materializer,
        )

    def test_accepted_vision_materializes_exactly_after_gate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            coordinator = Coordinator(
                db,
                storage,
                _VisionResolved(),
                _Studio(storage),
                _Publisher(),
                source=None,
            )
            calls = []

            async def materialize(target):
                calls.append(target)
                target.write_bytes(b"ORIGINAL")

            message = self._message("gate-accepted", materialize)
            try:
                item_id, materialized, item = asyncio.run(
                    coordinator._vision_gate_and_materialize_async(message)
                )
                self.assertEqual(item_id, "gate-accepted")
                self.assertTrue(materialized)
                self.assertEqual(len(calls), 1)
                self.assertTrue(item.original_path.is_file())
                self.assertEqual(item.original_path.read_bytes(), b"ORIGINAL")
                self.assertEqual(item.affiliate_url, "https://affiliate.invalid/product")
                self.assertEqual(db.get(item_id).state, State.RECEIVED)
            finally:
                coordinator.close()

    def test_unresolved_vision_never_materializes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            coordinator = Coordinator(
                db,
                storage,
                _VisionUnresolved(),
                _Studio(storage),
                _Publisher(),
                source=None,
            )
            calls = []

            async def materialize(target):
                calls.append(target)
                target.write_bytes(b"MUST-NOT-HAPPEN")

            message = self._message("gate-waiting", materialize)
            try:
                item_id, materialized, item = asyncio.run(
                    coordinator._vision_gate_and_materialize_async(message)
                )
                self.assertEqual(item_id, "gate-waiting")
                self.assertFalse(materialized)
                self.assertEqual(calls, [])
                self.assertEqual(item.state, State.WAITING_VISION)
                self.assertFalse(item.original_path.exists())
            finally:
                coordinator.close()

    def test_stop_after_vision_is_a_hard_boundary_for_already_approved_rows(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            studio = _Studio(storage)
            publisher = _Publisher()
            coordinator = Coordinator(
                db,
                storage,
                _VisionResolved(),
                studio,
                publisher,
                source=None,
            )
            try:
                item_id = db.reserve_item(
                    "already-approved",
                    source_id="telegram",
                    original_url="https://shopee.com.br/product/123",
                    original_path=storage.original("already-approved"),
                )
                db.set_vision(
                    item_id,
                    "Produto",
                    "https://affiliate.invalid/product",
                    affiliate_urls=("https://affiliate.invalid/product",),
                )

                coordinator.pipeline.run(item_id, stop_after_vision=True)

                self.assertEqual(db.get(item_id).state, State.RECEIVED)
                self.assertFalse(db.get(item_id).original_path.exists())
                self.assertEqual(studio.calls, 0)
                self.assertEqual(publisher.calls, 0)
            finally:
                coordinator.close()

    def test_blank_affiliate_url_is_revalidated_before_any_production_stage(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            studio = _Studio(storage)
            coordinator = Coordinator(
                db,
                storage,
                _VisionResolved(),
                studio,
                _Publisher(),
                source=None,
            )
            try:
                item_id = db.reserve_item(
                    "blank-affiliate",
                    source_id="telegram",
                    original_url="https://shopee.com.br/product/123",
                    original_path=storage.original("blank-affiliate"),
                )
                db.set_vision(item_id, "produto antigo", "   ")

                coordinator.pipeline.run(item_id, stop_after_vision=True)

                item = db.get(item_id)
                self.assertEqual(item.state, State.RECEIVED)
                self.assertEqual(item.affiliate_url, "https://affiliate.invalid/product")
                self.assertFalse(item.original_path.exists())
                self.assertEqual(studio.calls, 0)
            finally:
                coordinator.close()

    def test_technical_vision_failure_never_downloads(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            coordinator = Coordinator(
                db,
                storage,
                _VisionTechnicalFailure(),
                _Studio(storage),
                _Publisher(),
                source=None,
            )
            calls = []

            async def materialize(target):
                calls.append(target)
                target.write_bytes(b"MUST-NOT-HAPPEN")

            message = self._message("gate-technical", materialize)
            try:
                with self.assertRaisesRegex(RuntimeError, "vision-provider-timeout"):
                    asyncio.run(
                        coordinator._vision_gate_and_materialize_async(message)
                    )
                item = db.get("gate-technical")
                self.assertEqual(item.state, State.VISION)
                self.assertIn("vision-provider-timeout", db.last_error(item.content_id))
                self.assertEqual(calls, [])
                self.assertFalse(item.original_path.exists())
            finally:
                coordinator.close()

    def test_technical_vision_recovery_rediscovery_can_retry_before_download(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")

            class Vision:
                def __init__(self):
                    self.calls = 0

                def identify(self, item):
                    self.calls += 1
                    if self.calls == 1:
                        raise RuntimeError("vision-provider-timeout")
                    return VisionResult(
                        "Produto",
                        "https://affiliate.invalid/product",
                        affiliate_urls=("https://affiliate.invalid/product",),
                        ia_context={"productName": "Produto"},
                    )

            class Source:
                def __init__(self):
                    self.scan_calls = 0
                    self.reset_calls = 0
                    self.returned = False
                    self.exhausted = False

                async def fetch_next_async(self):
                    if self.returned:
                        self.exhausted = True
                        return None
                    self.returned = True
                    async def materialize(target):
                        target.write_bytes(b"ORIGINAL")
                    return IngestMessage(
                        telegram_message_id="gate-rediscover",
                        source_id="telegram",
                        topic_id=None,
                        topic_name="topic",
                        original_url="https://shopee.com.br/product/123",
                        materialize=materialize,
                    )

                def reset_historical_scan(self):
                    self.reset_calls += 1
                    self.returned = False
                    self.exhausted = False

                @property
                def historical_scan_exhausted(self):
                    return self.exhausted

                @property
                def historical_materialization_failed(self):
                    return False

                def mark_ingested(self, _message_id):
                    pass

                def complete_historical_sync(self):
                    db.complete_historical_sync()

                def commit_live_checkpoints(self, _checkpoints):
                    pass

            vision = Vision()
            source = Source()
            coordinator = Coordinator(
                db,
                storage,
                vision,
                _Studio(storage),
                _Publisher(),
                source=source,
            )
            try:
                asyncio.run(coordinator._run_catch_up_with_recovery_async())
                item = db.get("gate-rediscover")
                self.assertEqual(source.reset_calls, 0)
                self.assertEqual(vision.calls, 1)
                self.assertEqual(item.state, State.VISION)
                self.assertFalse(item.original_path.is_file())
                self.assertFalse(db.historical_complete())
            finally:
                coordinator.close()


    def test_pre_download_technical_failure_stays_retryable_without_original(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            coordinator = Coordinator(
                db,
                storage,
                _VisionTechnicalFailure(),
                _Studio(storage),
                _Publisher(),
                source=None,
            )
            try:
                item_id = db.reserve_item(
                    "retryable-vision",
                    source_id="telegram",
                    original_url="https://shopee.com.br/product/retryable",
                    original_path=storage.original("retryable-vision"),
                )
                with self.assertRaisesRegex(RuntimeError, "vision-provider-timeout"):
                    coordinator.pipeline.run(item_id, stop_after_vision=True)

                item = db.get(item_id)
                self.assertEqual(item.state, State.VISION)
                self.assertFalse(item.original_path.is_file())
                self.assertIn("vision-provider-timeout", db.last_error(item_id))

                coordinator.pipeline.vision = _VisionResolved()
                coordinator.pipeline.run(item_id, stop_after_vision=True)
                item = db.get(item_id)
                self.assertEqual(item.state, State.RECEIVED)
                self.assertEqual(item.affiliate_url, "https://affiliate.invalid/product")
                self.assertFalse(item.original_path.is_file())
            finally:
                coordinator.close()


if __name__ == "__main__":
    unittest.main()

    def test_multisource_catchup_keeps_recovery_row_when_vision_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")

            class Source:
                def __init__(self):
                    self.sent = False
                    self.exhausted = False
                    self.failed = False

                async def fetch_next_async(self):
                    if self.sent:
                        self.exhausted = True
                        return None
                    self.sent = True
                    async def materialize(target):
                        target.write_bytes(b"MUST-NOT-HAPPEN")
                    from types import SimpleNamespace
                    return SimpleNamespace(
                        telegram_message_id="77",
                        source_id="-1002039708059",
                        topic_id=5,
                        topic_name="source-three",
                        original_url="https://shopee.com.br/product/77",
                        materialize=materialize,
                    )

                @property
                def historical_scan_exhausted(self):
                    return self.exhausted

                @property
                def historical_materialization_failed(self):
                    return self.failed

                def mark_materialization_failed(self):
                    self.failed = True

                def mark_ingested(self, _message_id):
                    pass

                def commit_live_checkpoints(self, _checkpoints):
                    raise AssertionError("checkpoint must not advance after technical Vision failure")

                def complete_historical_sync(self):
                    raise AssertionError("history must not complete while Recovery is pending")

            source = Source()
            coordinator = Coordinator(
                db,
                storage,
                _VisionTechnicalFailure(),
                _Studio(storage),
                _Publisher(),
                source=source,
            )
            try:
                item_id = str(db.content_id_for("77", "-1002039708059"))
                processed = asyncio.run(coordinator.run_catch_up_async())
                item = db.get(item_id)
                self.assertEqual(processed, [item_id])
                self.assertEqual(item.state, State.RECOVERY)
                self.assertFalse(item.original_path.exists())
                self.assertTrue(source.failed)
            finally:
                coordinator.close()

    def test_staged_historical_catchup_finishes_vision_pass_before_any_download(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            events = []

            class Vision:
                def identify(self, item):
                    events.append(f"vision:{item.telegram_message_id}")
                    if item.telegram_message_id == "2":
                        raise VisionUnresolvedError("produto não resolvido")
                    return VisionResult(
                        "Produto",
                        "https://affiliate.invalid/product/1",
                        affiliate_urls=("https://affiliate.invalid/product/1",),
                        ia_context={"productName": "Produto"},
                    )

            class Studio(_Studio):
                def process(self, item):
                    events.append(f"studio:{item.telegram_message_id}")
                    return super().process(item)

            class Publisher(_Publisher):
                def publish(self, item):
                    events.append(f"publish:{item.telegram_message_id}")
                    return super().publish(item)

            class Source:
                source_id = "telegram"

                def __init__(self):
                    self.exhausted = False
                    self.failed = False
                    self.completed = False

                async def iter_historical_candidates_async(self):
                    from types import SimpleNamespace
                    for message_id in ("1", "2"):
                        async def ignored_materializer(target):
                            raise AssertionError("Vision pass must not download")
                        yield SimpleNamespace(
                            telegram_message_id=message_id,
                            source_id=self.source_id,
                            topic_id=1,
                            topic_name="topic",
                            original_url=f"https://shopee.com.br/product/{message_id}",
                            materialize=ignored_materializer,
                        )
                    self.exhausted = True

                async def materialize_candidate_async(self, message_id, target):
                    events.append(f"download:{message_id}")
                    target.write_bytes(b"ORIGINAL")

                @property
                def historical_scan_exhausted(self):
                    return self.exhausted

                @property
                def historical_materialization_failed(self):
                    return self.failed

                def mark_ingested(self, _message_id):
                    pass

                def mark_materialization_failed(self):
                    self.failed = True

                def complete_historical_sync(self):
                    self.completed = True
                    db.complete_historical_sync()

            source = Source()
            coordinator = Coordinator(
                db, storage, Vision(), Studio(storage), Publisher(), source=source
            )
            try:
                processed = asyncio.run(coordinator.run_catch_up_async())
                self.assertIn("vision:1", events)
                self.assertIn("vision:2", events)
                self.assertLess(events.index("vision:2"), events.index("download:1"))
                self.assertEqual(events.count("download:1"), 1)
                self.assertNotIn("download:2", events)
                self.assertEqual(db.get("1").state, State.PUBLISHED)
                self.assertEqual(db.get("2").state, State.WAITING_VISION)
                self.assertTrue(source.completed)
                self.assertEqual(set(processed), {"1", "2"})
            finally:
                coordinator.close()

