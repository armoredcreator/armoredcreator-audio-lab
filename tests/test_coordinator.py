import tempfile
import unittest
from pathlib import Path

from armored_core.coordinator import Coordinator
from armored_core.database import Database
from armored_core.models import PublicationCheck, State
from armored_core.services import PublicationResult, StudioResult, VisionResult
from armored_core.storage import Storage
from armored_core.production_contracts import SourceMessage

class Source:
    def __init__(self, path):
        self.path = path
        self.used = False
    def fetch_next(self):
        if self.used:
            return None
        self.used = True
        return SourceMessage(self.path, "telegram-100")

class Vision:
    def identify(self, item):
        return VisionResult("affiliate", "https://example.invalid/affiliate")

class Studio:
    def __init__(self, storage):
        self.storage = storage
    def process(self, item):
        w = self.storage.working(item.content_id)
        r = self.storage.result(item.content_id, affiliate_name=item.affiliate_name or "affiliate")
        payload = item.original_path.read_bytes()
        w.write_bytes(payload)
        r.write_bytes(payload + b"-processed")
        return StudioResult(w, r)

class Publisher:
    def __init__(self):
        self.ids = set()
        self.count = 0
    def check_publication(self, item):
        return PublicationCheck.CONFIRMED if item.item_id in self.ids else PublicationCheck.ABSENT
    def publish(self, item):
        self.count += 1
        self.ids.add(item.item_id)
        return PublicationResult(True, f"telegram-result-{self.count}")

class AsyncSource:
    def __init__(self):
        self.used = False
        self.marked = False

    async def fetch_next_async(self):
        if self.used:
            return None
        self.used = True

        async def materialize(target):
            target.write_bytes(b"ASYNC-TELEGRAM")

        from armored_core.services import IngestMessage
        return IngestMessage(
            telegram_message_id="telegram-async-1",
            source_id="telegram",
            original_url="https://shopee.com.br/example/finaldomeulinknovo",
            materialize=materialize,
        )

    def mark_ingested(self, message_id):
        self.marked = message_id

class CoordinatorTests(unittest.TestCase):
    def test_async_source_materializes_inside_same_event_loop(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            source = AsyncSource()
            coordinator = Coordinator(db, storage, Vision(), Studio(storage), Publisher(), source)

            item_id = coordinator.ingest_once()

            self.assertEqual(item_id, "telegram-async-1")
            item = db.get(item_id)
            self.assertEqual(item.original_path.name, "telegram-async-1_finallinkoriginal.mp4")
            self.assertEqual(item.original_path.read_bytes(), b"ASYNC-TELEGRAM")
            self.assertFalse((root / "storage" / "sync").exists())
            self.assertEqual(source.marked, "telegram-async-1")
            coordinator.close()

    def test_interrupted_materialization_leaves_durable_received_item(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            service = __import__("armored_core.services", fromlist=["SyncService"]).SyncService(db, storage)

            async def materialize(target):
                target.write_bytes(b"PARTIAL")
                raise RuntimeError("simulated-download-crash")

            from armored_core.services import IngestMessage
            message = IngestMessage(
                telegram_message_id="telegram-crash",
                original_url="https://shopee.com.br/example/crash",
                materialize=materialize,
            )

            with self.assertRaises(RuntimeError):
                import asyncio
                asyncio.run(service.ingest_message_async(message))

            row = db.get("telegram-crash")
            self.assertEqual(row.state, State.RECEIVED)
            self.assertTrue(row.original_path.parent.exists())
            self.assertFalse(row.original_path.exists())
            self.assertFalse(row.original_path.with_suffix(row.original_path.suffix + ".part").exists())
            db.close()


    def test_live_connection_failure_does_not_kill_coordinator(self):
        class FlakyLiveSource:
            def __init__(self):
                self.calls = 0

            async def fetch_live_candidate_async(self):
                self.calls += 1
                if self.calls == 1:
                    raise ConnectionError("simulated Telegram outage")
                return None, {}

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            db.complete_historical_sync()
            db.set_sync_topic_checkpoint(0, "LIVE-TEST", 0)
            db.set_sync_mode("LIVE")
            source = FlakyLiveSource()
            coordinator = Coordinator(
                db,
                storage,
                Vision(),
                Studio(storage),
                Publisher(),
                source,
            )

            coordinator.run_forever(poll_seconds=0.001, max_cycles=2)

            self.assertEqual(source.calls, 2)
            coordinator.close()

    def test_complete_chain_is_composed_and_sequential(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            src = root / "input.mp4"
            src.write_bytes(b"ORIGINAL")
            pub = Publisher()
            coordinator = Coordinator(db, storage, Vision(), Studio(storage), pub, Source(src))

            item_id = coordinator.process_next()

            self.assertEqual(item_id, "telegram-100")
            row = db.get(item_id)
            self.assertEqual(row.state, State.PUBLISHED)
            self.assertEqual(row.original_path.read_bytes(), b"ORIGINAL")
            self.assertEqual(pub.count, 1)
            self.assertIsNone(coordinator.process_next())
            self.assertEqual(pub.count, 1)
            coordinator.close()

if __name__ == "__main__":
    unittest.main()
