import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from ArmoredSync.service import TelegramSource
from armored_core.coordinator import Coordinator
from armored_core.database import Database
from armored_core.storage import Storage


class _Reader:
    def __init__(self):
        self.client = self
        self.connected = False

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False


class _Db:
    def __init__(self):
        self.checkpoints = {}
        self.completed = False

    def sync_mode(self):
        return "LIVE" if self.completed else "CATCH_UP"

    def set_sync_topic_checkpoint(self, topic_id, topic_name, message_id):
        self.checkpoints[int(topic_id)] = int(message_id)

    def complete_historical_sync(self):
        self.completed = True


def test_certification_cutover_advances_each_topic_to_current_high_water_mark(tmp_path, monkeypatch):
    monkeypatch.setenv("ARMORED_SYNC_SOURCE", "Teste")
    reader = _Reader()
    db = _Db()
    source = TelegramSource(tmp_path, reader, db)

    async def discover(_source):
        return [(10, "Teste"), (20, "Outro")]

    async def topic_messages(_source, topic_id):
        from types import SimpleNamespace

        newest = 1000 if topic_id == 10 else 2000
        yield SimpleNamespace(id=newest, video=False, message="", entities=[])

    source._discover_topics = discover
    source._topic_messages = topic_messages

    checkpoints = asyncio.run(source.prepare_live_cutover_async())

    assert checkpoints == {10: 1000, 20: 2000}
    assert db.checkpoints == {10: 1000, 20: 2000}
    assert db.completed is True
    assert source.is_historical_complete() is True
    assert reader.connected is True

    asyncio.run(reader.disconnect())
    assert reader.connected is False


class _CertificationSource:
    _historical_limit = 3

    def __init__(self, db):
        self.db = db
        self.cutover_called = False
        self.live_called = False

    async def prepare_live_cutover_async(self):
        self.cutover_called = True
        self.db.complete_historical_sync()

    async def fetch_live_candidate_async(self):
        self.live_called = True
        return None, {}


class _BoundedCoordinator(Coordinator):
    async def run_catch_up_async(self):
        self._last_catch_up_completed_count = 3
        return ["1", "2", "3"]


class CertificationCatchupThenLiveTests(unittest.TestCase):
    def test_explicit_certification_flag_cuts_over_to_live(self):
        previous = os.environ.get("ARMORED_CERT_CATCHUP_THEN_LIVE")
        os.environ["ARMORED_CERT_CATCHUP_THEN_LIVE"] = "1"

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            source = _CertificationSource(db)
            coordinator = _BoundedCoordinator(
                db, storage, object(), object(), object(), source
            )
            try:
                coordinator.run_forever(max_cycles=1, poll_seconds=0)
                assert source.cutover_called is True
                assert source.live_called is True
                assert db.historical_complete() is True
            finally:
                coordinator.close()

        if previous is None:
            os.environ.pop("ARMORED_CERT_CATCHUP_THEN_LIVE", None)
        else:
            os.environ["ARMORED_CERT_CATCHUP_THEN_LIVE"] = previous
