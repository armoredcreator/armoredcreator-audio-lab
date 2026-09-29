import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from armored_core.coordinator import Coordinator
from armored_core.database import Database
from armored_core.models import PublicationCheck, State
from armored_core.services import PublicationResult, StudioResult, VisionResult
from armored_core.storage import Storage


class IncrementalSource:
    def __init__(self):
        self.messages = ["telegram-1", "telegram-2"]
        self.index = 0
        self.marked = []
        self.historical_complete = False

    async def fetch_next_async(self):
        if self.index >= len(self.messages):
            self.mark_historical_complete()
            return None
        message_id = self.messages[self.index]
        self.index += 1

        async def materialize(target):
            target.write_bytes(message_id.encode())

        return SimpleNamespace(
            telegram_message_id=message_id,
            source_id="telegram",
            topic_id=228,
            topic_name="Telegram 228",
            original_url=("https://shopee.com.br/example/final/telegram-1" if message_id == "telegram-1" else "https://shopee.com.br/example/final/telegram-2"),
            source_path=None,
            materialize=materialize,
        )

    def mark_historical_complete(self):
        self.historical_complete = True

    def mark_ingested(self, message_id):
        self.marked.append(str(message_id))


class Vision:
    def identify(self, item):
        return VisionResult("affiliate", "https://shopee.com.br/affiliate/final")


class Studio:
    def __init__(self, storage):
        self.storage = storage

    def process(self, item):
        working = self.storage.working(item.content_id)
        result = self.storage.result(item.content_id, affiliate_name=item.affiliate_name or "affiliate")
        payload = item.original_path.read_bytes()
        working.write_bytes(payload)
        result.write_bytes(payload + b"-final")
        return StudioResult(working, result)


class Publisher:
    def __init__(self):
        self.published = []

    def check_publication(self, item):
        return PublicationCheck.ABSENT

    def publish(self, item):
        self.published.append(item.item_id)
        return PublicationResult(True, "published-" + item.item_id)


class BatchTelegramSource:
    def __init__(self):
        self.connected = True
        self.marked = []
        self.disconnected = False

    async def collect_historical_batch_async(self):
        async def materialize(target):
            if self.disconnected:
                raise AssertionError("historical materialization occurred after Telegram disconnect")
            target.write_bytes(b"BATCH-TELEGRAM")

        return [
            SimpleNamespace(
                telegram_message_id="telegram-batch-1",
                source_id="telegram",
                topic_id=101,
                topic_name="Batch",
                original_url="https://shopee.com.br/example/batch",
                source_path=None,
                materialize=materialize,
            )
        ], {101: 900}

    def mark_ingested(self, message_id):
        self.marked.append(str(message_id))

    async def disconnect(self):
        self.disconnected = True

    def commit_live_checkpoints(self, checkpoints):
        self.checkpoints = dict(checkpoints)



class IncrementalCoordinatorTests(unittest.TestCase):
    def test_catch_up_processes_each_item_incrementally(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            source = IncrementalSource()
            publisher = Publisher()
            coordinator = Coordinator(db, storage, Vision(), Studio(storage), publisher, source)

            processed = coordinator.run_catch_up()

            self.assertEqual(processed, ["telegram-1", "telegram-2"])
            self.assertEqual(source.marked, ["telegram-1", "telegram-2"])
            self.assertTrue(db.historical_complete())
            self.assertEqual(db.get("telegram-1").state, State.PUBLISHED)
            self.assertEqual(db.get("telegram-2").state, State.PUBLISHED)
            self.assertEqual(publisher.published, ["telegram-1", "telegram-2"])
            coordinator.close()


    def test_real_source_materializes_one_candidate_before_processing(self):
        class SingleCandidateTelegramSource:
            def __init__(self):
                self.connected = False
                self.disconnected = False
                self.marked = []
                self.checkpoints = {}

            async def fetch_next_async(self):
                async def materialize(target):
                    if self.disconnected:
                        raise AssertionError("materialization occurred after disconnect")
                    target.write_bytes(b"SINGLE-CANDIDATE")
                return SimpleNamespace(
                    telegram_message_id="901",
                    source_id="telegram",
                    topic_id=101,
                    topic_name="Single",
                    original_url="https://shopee.com.br/example/single",
                    source_path=None,
                    materialize=materialize,
                ) if not self.marked else None

            def mark_ingested(self, message_id):
                self.marked.append(str(message_id))

            async def disconnect(self):
                self.disconnected = True

            def commit_live_checkpoints(self, checkpoints):
                self.checkpoints = dict(checkpoints)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            source = SingleCandidateTelegramSource()
            publisher = Publisher()
            coordinator = Coordinator(db, storage, Vision(), Studio(storage), publisher, source)

            try:
                processed = coordinator.run_catch_up()
                self.assertEqual(processed, ["901"])
                self.assertEqual(source.marked, ["901"])
                self.assertTrue(db.historical_complete())
                self.assertEqual(db.get("901").state, State.PUBLISHED)
                self.assertEqual(publisher.published, ["901"])
            finally:
                coordinator.close()



    def test_catch_up_does_not_complete_after_pipeline_failure(self):
        class FailingSource:
            def __init__(self, db):
                self.db = db
                self.done = False
                self.historical_scan_exhausted = False
                self.completed = False

            async def fetch_next_async(self):
                if self.done:
                    self.historical_scan_exhausted = True
                    return None
                self.done = True

                async def materialize(target):
                    target.write_bytes(b"FAIL")

                return SimpleNamespace(
                    telegram_message_id="902",
                    source_id="telegram",
                    topic_id=101,
                    topic_name="Failure",
                    original_url="https://shopee.com.br/example/failure",
                    materialize=materialize,
                )

            def mark_ingested(self, message_id):
                pass

            async def disconnect(self):
                pass

            def commit_live_checkpoints(self, checkpoints):
                for topic_id, message_id in checkpoints.items():
                    self.db.set_sync_topic_checkpoint(topic_id, "Failure", message_id)

            def complete_historical_sync(self):
                self.completed = True
                self.db.complete_historical_sync()

        class FailingCoordinator(Coordinator):
            def run(self, item_id: str) -> None:
                raise RuntimeError("synthetic pipeline failure")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            source = FailingSource(db)
            coordinator = FailingCoordinator(
                db, storage, Vision(), Studio(storage), Publisher(), source
            )

            try:
                processed = coordinator.run_catch_up()
                self.assertEqual(processed, ["902"])
                self.assertFalse(source.completed)
                self.assertFalse(db.historical_complete())
                self.assertEqual(db.get("902").state, State.RECEIVED)
            finally:
                coordinator.close()



    def test_catch_up_continues_after_technical_recovery_failure(self):
        class RecoveryThenSuccessSource:
            def __init__(self, db):
                self.db = db
                self.calls = 0
                self.completed = False
                self.checkpoints = []

            async def fetch_next_async(self):
                self.calls += 1
                if self.calls == 1:
                    async def materialize(target):
                        target.write_bytes(b"RECOVERY-FIRST")
                    return SimpleNamespace(
                        telegram_message_id="903",
                        source_id="telegram",
                        topic_id=101,
                        topic_name="Recovery",
                        original_url="https://shopee.com.br/example/903",
                        source_path=None,
                        materialize=materialize,
                    )
                if self.calls == 2:
                    async def materialize(target):
                        target.write_bytes(b"SUCCESS-SECOND")
                    return SimpleNamespace(
                        telegram_message_id="904",
                        source_id="telegram",
                        topic_id=101,
                        topic_name="Recovery",
                        original_url="https://shopee.com.br/example/904",
                        source_path=None,
                        materialize=materialize,
                    )
                return None

            def mark_ingested(self, _message_id):
                return None

            async def disconnect(self):
                return None

            def commit_live_checkpoints(self, checkpoints):
                self.checkpoints.append(dict(checkpoints))

            def complete_historical_sync(self):
                self.completed = True
                self.db.complete_historical_sync()

        class FailureCoordinator(Coordinator):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.run_calls = []

            def run(self, item_id: str) -> None:
                self.run_calls.append(str(item_id))
                if str(item_id) == "903":
                    self.db.transition(
                        item_id,
                        State.RECOVERY,
                        "CaptionTransportError: Gemini indisponível",
                    )
                    raise RuntimeError("Caption Gemini indisponível")
                self.db.transition(item_id, State.PUBLISHED, "test-published")
                self.db.mark_cleanup_completed(item_id)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            source = RecoveryThenSuccessSource(db)
            coordinator = FailureCoordinator(
                db, storage, Vision(), Studio(storage), Publisher(), source
            )

            try:
                processed = coordinator.run_catch_up()
                self.assertEqual(processed, ["903", "904"])
                self.assertEqual(source.calls, 3)
                self.assertEqual(coordinator.run_calls, ["903", "904"])
                self.assertEqual(db.get("903").state, State.RECOVERY)
                self.assertEqual(db.get("904").state, State.PUBLISHED)
                self.assertTrue(db.get("904").cleanup_completed)
                self.assertFalse(db.historical_complete())
                self.assertEqual(source.checkpoints, [])
            finally:
                coordinator.close()

    def test_rediscovered_completed_candidate_can_advance_checkpoint(self):
        class CompletedSource:
            def __init__(self, db):
                self.db = db
                self.calls = 0
                self.checkpoints = []

            async def fetch_next_async(self):
                self.calls += 1
                if self.calls in (1, 2):
                    async def materialize(target):
                        target.write_bytes(b"ALREADY-DONE")
                    return SimpleNamespace(
                        telegram_message_id="905",
                        source_id="telegram",
                        topic_id=102,
                        topic_name="Checkpoint",
                        original_url="https://shopee.com.br/example/905",
                        source_path=None,
                        materialize=materialize,
                    )
                return None

            def mark_ingested(self, _message_id):
                return None

            async def disconnect(self):
                return None

            def commit_live_checkpoints(self, checkpoints):
                self.checkpoints.append(dict(checkpoints))

            def complete_historical_sync(self):
                self.db.complete_historical_sync()

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            source = CompletedSource(db)
            coordinator = Coordinator(db, storage, Vision(), Studio(storage), Publisher(), source)
            item_id = coordinator.ingest_once()
            db.transition(item_id, State.VISION, "test")
            db.transition(item_id, State.STUDIO, "test")
            db.transition(item_id, State.PUBLISHING, "test")
            db.transition(item_id, State.PUBLISHED, "test")
            db.mark_cleanup_completed(item_id)

            try:
                processed = coordinator.run_catch_up()
                self.assertEqual(processed, [])
                self.assertEqual(source.checkpoints, [{102: 905}])
                self.assertTrue(db.historical_complete())
            finally:
                coordinator.close()


if __name__ == "__main__":
    unittest.main()
