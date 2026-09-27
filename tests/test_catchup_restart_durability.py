import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from armored_core.coordinator import Coordinator
from armored_core.database import Database
from armored_core.models import PublicationCheck
from armored_core.services import PublicationResult, StudioResult, VisionResult
from armored_core.storage import Storage


class _Vision:
    def identify(self, item):
        return VisionResult("affiliate", "https://example.invalid/affiliate")


class _Studio:
    def __init__(self, storage):
        self.storage = storage

    def process(self, item):
        result = self.storage.result(item.content_id, affiliate_url=item.affiliate_url)
        result.write_bytes(item.original_path.read_bytes() + b"-final")
        return StudioResult(None, result)


class _Publisher:
    def __init__(self, crash_first=False):
        self.published = []
        self.calls = 0
        self.crash_first = crash_first

    def check_publication(self, item):
        return PublicationCheck.ABSENT

    def publish(self, item):
        self.calls += 1
        if self.crash_first and self.calls == 1:
            raise RuntimeError("simulated-process-crash")
        self.published.append(item.content_id)
        return PublicationResult(True, "pub-" + item.content_id)


class _SequentialSource:
    def __init__(self, db, files):
        self.db = db
        self.files = files
        self.index = 0
        self.connects = 0
        self.disconnects = 0
        self.materializations = []
        self.checkpoints = []

    async def fetch_next_async(self):
        if self.index >= len(self.files):
            return None
        message_id = list(self.files)[self.index]
        self.index += 1

        async def materialize(target, message_id=message_id):
            self.materializations.append(("start", message_id))
            target.write_bytes(self.files[message_id].read_bytes())
            self.materializations.append(("end", message_id))

        return SimpleNamespace(
            telegram_message_id=message_id,
            source_id="telegram",
            topic_id=10,
            topic_name="topic",
            original_url="https://shopee.example/" + message_id,
            materialize=materialize,
        )

    async def connect(self):
        self.connects += 1

    async def disconnect(self):
        self.disconnects += 1

    def mark_ingested(self, message_id):
        pass

    def commit_live_checkpoints(self, checkpoints):
        self.checkpoints.append(dict(checkpoints))
        for topic_id, message_id in checkpoints.items():
            self.db.set_sync_topic_checkpoint(topic_id, "topic", message_id)


class CatchUpRestartTests(unittest.TestCase):
    def test_catchup_materializes_and_processes_one_item_before_next_download(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            files = {}
            for message_id in ("301", "302"):
                path = root / f"{message_id}.mp4"
                path.write_bytes(message_id.encode())
                files[message_id] = path

            source = _SequentialSource(db, files)
            publisher = _Publisher()
            coordinator = Coordinator(db, storage, _Vision(), _Studio(storage), publisher, source)

            events = []
            original_run = coordinator.run

            def run(item_id):
                events.append(("pipeline", str(item_id)))
                original_run(item_id)

            coordinator.run = run
            try:
                processed = coordinator.run_catch_up()
                self.assertEqual(processed, ["301", "302"])
                self.assertEqual(publisher.published, ["301", "302"])
                self.assertEqual(source.materializations,
                                 [("start", "301"), ("end", "301"),
                                  ("start", "302"), ("end", "302")])
                self.assertEqual(source.checkpoints, [{10: 301}, {10: 302}])
                self.assertEqual(events, [("pipeline", "301"), ("pipeline", "302")])
                self.assertEqual(source.connects, 0)
                # This fake never exposes a reader connection, so Coordinator
                # must not call a nonexistent/unused disconnect lifecycle.
                self.assertEqual(source.disconnects, 0)
            finally:
                coordinator.close()

    def test_restart_keeps_durable_original_after_processing_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            source_file = root / "301.mp4"
            source_file.write_bytes(b"301")
            source = _SequentialSource(db, {"301": source_file})
            first = Coordinator(db, storage, _Vision(), _Studio(storage), _Publisher(crash_first=True), source)

            try:
                processed = first.run_catch_up()
                self.assertEqual(processed, ["301"])
                self.assertEqual(db.get("301").state.value, "RECOVERY")
                self.assertTrue(db.get("301").original_path.is_file())
            finally:
                first.close()

            restarted_db = Database(storage.database / "db.sqlite")
            restarted = Coordinator(
                restarted_db, storage, _Vision(), _Studio(storage),
                _Publisher(), _SequentialSource(restarted_db, {"301": source_file})
            )
            try:
                restarted.recover("301")
                self.assertEqual(restarted_db.get("301").state.value, "PUBLISHED")
                self.assertTrue(restarted_db.get("301").original_path.is_file())
            finally:
                restarted.close()

    def test_download_failure_does_not_advance_checkpoint_past_failed_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")

            files = {}
            for message_id in ("401", "402"):
                path = root / f"{message_id}.mp4"
                path.write_bytes(message_id.encode())
                files[message_id] = path

            source = _SequentialSource(db, files)
            original_fetch = source.fetch_next_async
            attempts = {"401": 0}

            async def fetch():
                message = await original_fetch()
                if message and message.telegram_message_id == "401":
                    attempts["401"] += 1
                    async def fail(target):
                        raise TimeoutError("simulated-download-timeout")
                    message = SimpleNamespace(
                        telegram_message_id=message.telegram_message_id,
                        source_id=message.source_id,
                        topic_id=message.topic_id,
                        topic_name=message.topic_name,
                        original_url=message.original_url,
                        materialize=fail,
                    )
                return message

            source.fetch_next_async = fetch
            coordinator = Coordinator(db, storage, _Vision(), _Studio(storage), _Publisher(), source)
            try:
                processed = coordinator.run_catch_up()
                self.assertEqual(processed, ["402"])
                self.assertFalse(db.historical_complete())
                self.assertEqual(db.sync_topic_checkpoint(10), 0)
                self.assertTrue(db.get("401").original_path.parent.exists())
                self.assertFalse(db.get("401").original_path.is_file())
                self.assertEqual(attempts["401"], 1)
            finally:
                coordinator.close()


if __name__ == "__main__":
    unittest.main()
