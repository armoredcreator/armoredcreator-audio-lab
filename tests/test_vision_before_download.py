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
                self.assertEqual(item.state, State.RECOVERY)
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
                self.assertEqual(source.reset_calls, 1)
                self.assertEqual(vision.calls, 2)
                self.assertEqual(item.state, State.PUBLISHED)
                self.assertTrue(item.original_path.is_file())
                self.assertTrue(db.historical_complete())
            finally:
                coordinator.close()


if __name__ == "__main__":
    unittest.main()
