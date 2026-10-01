import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from armored_core.coordinator import Coordinator
from armored_core.database import Database
from armored_core.models import PublicationCheck, State
from armored_core.services import PublicationResult, StudioResult, VisionResult
from armored_core.storage import Storage


class _Vision:
    def identify(self, item):
        return VisionResult("affiliate", "https://shopee.example/affiliate")


class _Studio:
    def __init__(self, storage):
        self.storage = storage

    def process(self, item):
        result = self.storage.result(item.content_id, affiliate_url=item.affiliate_url)
        result.write_bytes(item.original_path.read_bytes() + b"-final")
        return StudioResult(None, result)


class _Publisher:
    def __init__(self):
        self.published = []

    def check_publication(self, item):
        return PublicationCheck.ABSENT

    def publish(self, item):
        self.published.append(item.content_id)
        return PublicationResult(True, "telegram-" + item.content_id)


class _OneCandidatePerPoll:
    class _Reader:
        def __init__(self, owner):
            self.owner = owner
            self.connected = False

        def is_connected(self):
            return self.connected

        async def connect(self):
            self.connected = True
            self.owner.connect_count += 1

        async def disconnect(self):
            if self.connected:
                self.connected = False
                self.owner.disconnect_count += 1

    def __init__(self, db):
        self.db = db
        self.index = 0
        self.connect_count = 0
        self.disconnect_count = 0
        self.reader = self._Reader(self)

    async def fetch_live_batch_async(self, limit=None):
        candidates = ["live-1", "live-2"]
        if self.index >= len(candidates):
            return [], {}
        selected = candidates[self.index:self.index + (limit or len(candidates))]
        self.index += len(selected)

        async def materialize(target, value=selected[0]):
            target.write_bytes(value.encode())

        messages = [
            SimpleNamespace(
                telegram_message_id=value,
                source_id="telegram",
                topic_id=228,
                topic_name="topic",
                original_url="https://shopee.example/" + value,
                materialize=lambda target, value=value: materialize(target, value),
            )
            for value in selected
        ]
        return messages, {228: 100 + self.index}

    def mark_ingested(self, message_id):
        pass

    def commit_live_checkpoints(self, checkpoints):
        for topic_id, message_id in checkpoints.items():
            self.db.set_sync_topic_checkpoint(topic_id, "topic", message_id)


class LiveReconnectTests(unittest.TestCase):
    def test_live_discovers_materializes_disconnects_and_processes_one_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            db.complete_historical_sync()
            db.set_sync_topic_checkpoint(228, "topic", 99)

            source = _OneCandidatePerPoll(db)
            publisher = _Publisher()
            coordinator = Coordinator(db, storage, _Vision(), _Studio(storage), publisher, source)

            try:
                first = coordinator.run_live_once()
                self.assertEqual(first, ["live-1"])
                self.assertEqual(publisher.published, ["live-1"])
                self.assertEqual(source.connect_count, 1)
                self.assertEqual(source.disconnect_count, 1)
                self.assertEqual(db.get("live-1").state, State.PUBLISHED)
                self.assertIsNone(
                    db.conn.execute(
                        "SELECT 1 FROM items WHERE content_id='live-2'"
                    ).fetchone()
                )
            finally:
                coordinator.close()

    def test_run_forever_survives_transient_live_telegram_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "armoredcreator.db")
            db.complete_historical_sync()
            db.set_sync_topic_checkpoint(228, "topic", 99)

            class Source:
                class Reader:
                    def __init__(self):
                        self.connects = 0
                        self.disconnects = 0
                        self.connected = False

                    def is_connected(self):
                        return self.connected

                    async def connect(self):
                        self.connects += 1
                        self.connected = True

                    async def disconnect(self):
                        self.disconnects += 1
                        self.connected = False

                def __init__(self):
                    self.reader = self.Reader()
                    self.calls = 0

                async def fetch_live_candidate_async(self):
                    await self.reader.connect()
                    self.calls += 1
                    if self.calls == 1:
                        await self.reader.disconnect()
                        raise ConnectionError("telegram-temporarily-offline")
                    return None, {}

                def commit_live_checkpoints(self, checkpoints):
                    pass

            source = Source()
            coordinator = Coordinator(
                db,
                storage,
                _Vision(),
                _Studio(storage),
                _Publisher(),
                source,
            )
            previous = os.environ.get("ARMORED_LIVE_ERROR_BACKOFF")
            os.environ["ARMORED_LIVE_ERROR_BACKOFF"] = "1"
            try:
                coordinator.run_forever(max_cycles=2, poll_seconds=0)
            finally:
                if previous is None:
                    os.environ.pop("ARMORED_LIVE_ERROR_BACKOFF", None)
                else:
                    os.environ["ARMORED_LIVE_ERROR_BACKOFF"] = previous
                coordinator.close()

            self.assertEqual(source.calls, 2)
            self.assertEqual(source.reader.connects, 2)
            self.assertEqual(source.reader.disconnects, 1)
            self.assertEqual(db.sync_topic_checkpoint(228), 99)

    def test_next_poll_can_materialize_the_next_candidate_after_previous_completion(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            db.complete_historical_sync()
            db.set_sync_topic_checkpoint(228, "topic", 99)

            source = _OneCandidatePerPoll(db)
            publisher = _Publisher()
            coordinator = Coordinator(db, storage, _Vision(), _Studio(storage), publisher, source)
            try:
                self.assertEqual(coordinator.run_live_once(), ["live-1"])
                self.assertEqual(coordinator.run_live_once(), ["live-2"])
                self.assertEqual(publisher.published, ["live-1", "live-2"])
                self.assertEqual(source.connect_count, 2)
                self.assertEqual(source.disconnect_count, 2)
            finally:
                coordinator.close()


if __name__ == "__main__":
    unittest.main()
