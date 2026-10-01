import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from armored_core.database import Database
from armored_core.models import PublicationCheck
from armored_core.services import PublicationResult, StudioResult, VisionResult
from armored_core.storage import Storage
from armored_core.coordinator import Coordinator
from ArmoredSync.service import SyncMessage, TelegramSource


class FakeMessage:
    def __init__(self, message_id, video=False, text="", grouped_id=None):
        self.id = message_id
        self.video = video
        self.message = text
        self.entities = []
        self.grouped_id = grouped_id


class FakeReader:
    async def connect(self):
        pass

    async def disconnect(self):
        pass


class CandidateSource(TelegramSource):
    async def _discover_topics(self, source):
        return [(10, "topic")]

    async def _topic_messages(self, source, topic_id):
        messages = [
            FakeMessage(1, video=True),
            FakeMessage(2, text="https://shopee.com.br/x/abc"),
            FakeMessage(3, video=True, text="https://shopee.com.br/x/def"),
        ]
        for message in messages:
            yield message


class Vision:
    def identify(self, item):
        return VisionResult("affiliate", "https://example.invalid/affiliate")


class FailOnceVision:
    def __init__(self):
        self.failed = False

    def identify(self, item):
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated-live-crash")
        return VisionResult("affiliate", "https://example.invalid/affiliate")


class Studio:
    def __init__(self, storage):
        self.storage = storage

    def process(self, item):
        result = self.storage.result(item.content_id, affiliate_name=item.affiliate_name)
        result.write_bytes(item.original_path.read_bytes() + b"-processed")
        return StudioResult(None, result)


class Publisher:
    def __init__(self):
        self.published = []
        self.count = 0

    def check_publication(self, item):
        return PublicationCheck.ABSENT

    def publish(self, item):
        self.count += 1
        self.published.append(item.content_id)
        return PublicationResult(True, f"published-{item.content_id}")


class LifecycleSource:
    def __init__(self):
        self.history = [
            SyncMessage("100", source_id="telegram", source_path=None),
            SyncMessage("101", source_id="telegram", source_path=None),
        ]
        self.live = []
        self.index = 0
        self.live_called = False
        self.completed = False

    async def fetch_next_async(self):
        if self.index < len(self.history):
            value = self.history[self.index]
            self.index += 1
            return value
        self.completed = True
        return None

    async def collect_historical_batch_async(self):
        self.completed = True
        return self.history, {10: 101}

    async def fetch_live_batch_async(self):
        self.live_called = True
        return self.live, {}

    def mark_ingested(self, message_id):
        pass

    def is_historical_complete(self):
        return self.completed

    def commit_live_checkpoints(self, checkpoints):
        pass


class SyncLifecycleTests(unittest.TestCase):
    def test_database_starts_in_catch_up_and_persists_live(self):
        with tempfile.TemporaryDirectory() as td:
            db = Database(Path(td) / "db.sqlite")
            self.assertEqual(db.sync_mode(), "CATCH_UP")
            self.assertFalse(db.historical_complete())
            db.complete_historical_sync()
            db.close()

            reopened = Database(Path(td) / "db.sqlite")
            self.assertEqual(reopened.sync_mode(), "LIVE")
            self.assertTrue(reopened.historical_complete())
            reopened.close()

    def test_topic_checkpoint_is_monotonic(self):
        with tempfile.TemporaryDirectory() as td:
            db = Database(Path(td) / "db.sqlite")
            db.set_sync_topic_checkpoint(10, "topic", 100)
            db.set_sync_topic_checkpoint(10, "topic", 90)
            self.assertEqual(db.sync_topic_checkpoint(10), 100)
            db.set_sync_topic_checkpoint(10, "topic", 120)
            self.assertEqual(db.sync_topic_checkpoint(10), 120)
            db.close()

    def test_historical_candidate_uses_immediately_following_non_video(self):
        source = CandidateSource(Path("."), FakeReader())
        candidates = []

        async def collect():
            async for candidate in source._candidate_iterator("source", [(10, "topic")]):
                candidates.append(candidate)

        import asyncio
        asyncio.run(collect())

        self.assertEqual([candidate[0] for candidate in candidates], [1, 3])
        self.assertEqual(candidates[0][4], "https://shopee.com.br/x/abc")
        self.assertEqual(candidates[1][4], "https://shopee.com.br/x/def")

    def test_historical_candidate_resolves_shopee_from_same_grouped_album(self):
        source = CandidateSource(Path("."), FakeReader())
        source._topic_messages = None

        async def grouped_messages(source_name, topic_id):
            # Telegram history is newest-first: 452, 451, 450.
            messages = [
                FakeMessage(452, video=True, grouped_id=14295227350202649),
                FakeMessage(451, text="", grouped_id=14295227350202649),
                FakeMessage(
                    450,
                    text="https://s.shopee.com.br/5Ardb8fOzX",
                    grouped_id=14295227350202649,
                ),
            ]
            for message in messages:
                yield message

        source._topic_messages = grouped_messages

        candidates = []

        async def collect():
            async for candidate in source._candidate_iterator("source", [(287, "topic")]):
                candidates.append(candidate)

        asyncio.run(collect())

        self.assertEqual([candidate[0] for candidate in candidates], [452])
        self.assertEqual(
            candidates[0][4],
            "https://s.shopee.com.br/5Ardb8fOzX",
        )

    def test_historical_grouped_two_videos_one_link_emits_one_candidate(self):
        source = CandidateSource(Path("."), FakeReader())

        async def grouped_messages(source_name, topic_id):
            # Telegram history is newest-first: 493, 492, 491.
            messages = [
                FakeMessage(
                    493,
                    text="https://s.shopee.com.br/4fvTHpZbKQ",
                    grouped_id=14295237464830177,
                ),
                FakeMessage(492, video=True, grouped_id=14295237464830169),
                FakeMessage(
                    491,
                    video=True,
                    text="https://s.shopee.com.br/8piEHew9WS",
                    grouped_id=14295237464830169,
                ),
            ]
            for message in messages:
                yield message

        source._topic_messages = grouped_messages
        candidates = []

        async def collect():
            async for candidate in source._candidate_iterator("source", [(484, "topic")]):
                candidates.append(candidate)

        asyncio.run(collect())

        self.assertEqual([candidate[0] for candidate in candidates], [491])
        self.assertEqual(
            candidates[0][4],
            "https://s.shopee.com.br/8piEHew9WS",
        )

    def test_live_candidate_resolves_link_inside_group_and_advances_group_checkpoint(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            db.complete_historical_sync()
            db.set_sync_topic_checkpoint(567, "topic", 99)

            class Reader:
                def __init__(self):
                    self.connected = False

                    class Client:
                        async def iter_messages(inner, *args, **kwargs):
                            messages = [
                                FakeMessage(
                                    100,
                                    text="https://s.shopee.com.br/live-group",
                                    grouped_id=123456,
                                ),
                                FakeMessage(
                                    101,
                                    text="",
                                    grouped_id=123456,
                                ),
                                FakeMessage(
                                    102,
                                    video=True,
                                    grouped_id=123456,
                                ),
                            ]
                            for message in messages:
                                yield message

                    self.client = Client()

                async def connect(self):
                    self.connected = True

                async def disconnect(self):
                    self.connected = False

            class LiveGroupedSource(TelegramSource):
                async def _discover_topics(self, source):
                    return [(567, "topic")]

            reader = Reader()
            source = LiveGroupedSource(root, reader, db)

            message, checkpoints = asyncio.run(source.fetch_live_candidate_async())

            self.assertIsNotNone(message)
            self.assertEqual(message.telegram_message_id, "102")
            self.assertEqual(
                message.original_url,
                "https://s.shopee.com.br/live-group",
            )
            self.assertEqual(checkpoints, {567: 102})

            db.close()

    def test_iter_historical_candidates_uses_grouped_association(self):
        source = CandidateSource(Path("."), FakeReader())

        async def grouped_messages(source_name, topic_id):
            for message in [
                FakeMessage(302, video=True, grouped_id=9001),
                FakeMessage(301, grouped_id=9001),
                FakeMessage(300, text="https://s.shopee.com.br/grouped-iter", grouped_id=9001),
            ]:
                yield message

        source._topic_messages = grouped_messages
        old_mode = source._historical_complete
        candidates = []
        try:
            async def collect():
                async for message in source.iter_historical_candidates_async():
                    candidates.append(message)

            asyncio.run(collect())
        finally:
            source._historical_complete = old_mode

        self.assertEqual([message.telegram_message_id for message in candidates], ["302"])
        self.assertEqual(candidates[0].original_url, "https://s.shopee.com.br/grouped-iter")

    def test_collect_historical_batch_uses_grouped_association(self):
        source = CandidateSource(Path("."), FakeReader())

        async def grouped_messages(source_name, topic_id):
            for message in [
                FakeMessage(402, video=True, grouped_id=9002),
                FakeMessage(401, grouped_id=9002),
                FakeMessage(400, text="https://s.shopee.com.br/grouped-batch", grouped_id=9002),
            ]:
                yield message

        source._topic_messages = grouped_messages
        candidates, checkpoints = asyncio.run(source.collect_historical_batch_async())

        self.assertEqual([message.telegram_message_id for message in candidates], ["402"])
        self.assertEqual(candidates[0].original_url, "https://s.shopee.com.br/grouped-batch")
        self.assertEqual(checkpoints, {10: 402})

    def test_historical_grouped_duplicate_shopee_url_is_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            db.reserve_item(
                "already-there",
                original_url="https://s.shopee.com.br/duplicate",
                original_path=root / "already-there.mp4",
            )

            class ExistingUrlSource(TelegramSource):
                async def _topic_messages(self, source_name, topic_id):
                    for message in [
                        FakeMessage(502, video=True, grouped_id=9003),
                        FakeMessage(501, grouped_id=9003),
                        FakeMessage(500, text="https://s.shopee.com.br/duplicate", grouped_id=9003),
                    ]:
                        yield message

            source = ExistingUrlSource(root, FakeReader(), db)
            candidates = []

            async def collect():
                async for candidate in source._candidate_iterator("source", [(10, "topic")]):
                    candidates.append(candidate)

            asyncio.run(collect())

            self.assertEqual(candidates, [])
            self.assertEqual(source._historical_checkpoints, {10: 502})
            db.close()

    def test_live_group_with_two_distinct_links_does_not_skip_second_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            db.complete_historical_sync()
            db.set_sync_topic_checkpoint(567, "topic", 99)

            class Reader:
                def __init__(self):
                    class Client:
                        async def iter_messages(inner, *args, **kwargs):
                            for message in [
                                FakeMessage(100, video=True, text="https://s.shopee.com.br/live-a", grouped_id=9010),
                                FakeMessage(101, grouped_id=9010),
                                FakeMessage(102, video=True, text="https://s.shopee.com.br/live-b", grouped_id=9010),
                            ]:
                                yield message
                    self.client = Client()

                async def connect(self):
                    pass

                async def disconnect(self):
                    pass

            class LiveGroupedSource(TelegramSource):
                async def _discover_topics(self, source):
                    return [(567, "topic")]

            source = LiveGroupedSource(root, Reader(), db)

            first, first_cp = asyncio.run(source.fetch_live_candidate_async())
            self.assertIsNotNone(first)
            self.assertEqual(first.telegram_message_id, "100")
            self.assertEqual(first_cp, {567: 100})

            db.set_sync_topic_checkpoint(567, "topic", 100)
            second, second_cp = asyncio.run(source.fetch_live_candidate_async())
            self.assertIsNotNone(second)
            self.assertEqual(second.telegram_message_id, "102")
            self.assertEqual(second_cp, {567: 102})
            db.close()

    def test_live_duplicate_shopee_url_advances_safe_checkpoint_without_materializing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            db.complete_historical_sync()
            db.set_sync_topic_checkpoint(567, "topic", 99)
            db.reserve_item(
                "already-there",
                original_url="https://s.shopee.com.br/live-duplicate",
                original_path=root / "already-there.mp4",
            )

            class Reader:
                def __init__(self):
                    class Client:
                        async def iter_messages(inner, *args, **kwargs):
                            yield FakeMessage(
                                100,
                                video=True,
                                text="https://s.shopee.com.br/live-duplicate",
                            )
                    self.client = Client()

                async def connect(self):
                    pass

                async def disconnect(self):
                    pass

            class LiveSource(TelegramSource):
                async def _discover_topics(self, source):
                    return [(567, "topic")]

            source = LiveSource(root, Reader(), db)
            message, checkpoints = asyncio.run(source.fetch_live_candidate_async())

            self.assertIsNone(message)
            self.assertEqual(checkpoints, {567: 100})
            db.close()

    def test_coordinator_runs_history_then_live_with_same_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")

            for message_id in ("100", "101"):
                path = root / f"{message_id}.mp4"
                path.write_bytes(message_id.encode())
                self.assertTrue(path.is_file())

            source = LifecycleSource()
            source.history = [
                SyncMessage("100", source_id="local", source_path=root / "100.mp4",
                            original_url="https://shopee.com.br/x/a"),
                SyncMessage("101", source_id="local", source_path=root / "101.mp4",
                            original_url="https://shopee.com.br/x/b"),
            ]
            coordinator = Coordinator(db, storage, Vision(), Studio(storage), Publisher(), source)

            processed = coordinator.run_catch_up()
            self.assertEqual(processed, ["100", "101"])
            self.assertTrue(db.historical_complete())
            self.assertEqual(processed, ["100", "101"])

            live_path = root / "102.mp4"
            live_path.write_bytes(b"102")
            source.live = [SyncMessage("102", source_id="local", source_path=live_path,
                                       original_url="https://shopee.com.br/x/c")]

            live = coordinator.run_live_once()
            self.assertEqual(live, ["102"])
            self.assertTrue(source.live_called)
            self.assertEqual(db.get("102").telegram_message_id, "102")
            coordinator.close()

    def test_live_materializes_only_one_candidate_per_poll(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            db.complete_historical_sync()
            db.set_sync_topic_checkpoint(228, "topic", 99)

            class Reader:
                def __init__(self):
                    self.connected = False
                    self.connects = 0
                    self.disconnects = 0

                async def connect(self):
                    self.connected = True
                    self.connects += 1

                async def disconnect(self):
                    self.connected = False
                    self.disconnects += 1

                def is_connected(self):
                    return self.connected

            reader = Reader()

            class LiveSource(LifecycleSource):
                def __init__(self):
                    super().__init__()
                    self.reader = reader
                    self.completed = True
                    self.index = 0

                async def fetch_live_batch_async(self, limit=None):
                    await reader.connect()
                    values = ["201", "202"]
                    if self.index >= len(values):
                        return [], {}
                    value = values[self.index]
                    self.index += 1

                    async def materialize(target):
                        if not reader.connected:
                            raise RuntimeError("telegram-client-not-connected")
                        target.write_bytes(value.encode())

                    return [SyncMessage(
                        value, source_id="telegram", topic_id=228,
                        topic_name="topic",
                        original_url=f"https://shopee.com.br/x/live/{value}",
                        materialize=materialize,
                    )], {228: 99 + self.index}

                async def disconnect(self):
                    await reader.disconnect()

            source = LiveSource()
            publisher = Publisher()
            coordinator = Coordinator(db, storage, Vision(), Studio(storage), publisher, source)

            try:
                self.assertEqual(coordinator.run_live_once(), ["201"])
                self.assertEqual(publisher.published, ["201"])
                self.assertEqual(reader.connects, 1)
                self.assertEqual(reader.disconnects, 1)
                self.assertEqual(coordinator.run_live_once(), ["202"])
                self.assertEqual(publisher.published, ["201", "202"])
                self.assertEqual(reader.connects, 2)
                self.assertEqual(reader.disconnects, 2)
            finally:
                coordinator.close()


    def test_live_checkpoint_survives_processing_failure_without_startup_retry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            db.complete_historical_sync()

            source_file = root / "200.mp4"
            source_file.write_bytes(b"200")
            source = LifecycleSource()
            source.completed = True
            source.live = [SyncMessage(
                "200", source_id="local", source_path=source_file,
                original_url="https://shopee.com.br/x/restart",
            )]
            publisher = Publisher()
            first = Coordinator(
                db, storage, FailOnceVision(), Studio(storage), publisher, source
            )

            # Processing failures are isolated by the continuous Coordinator:
            # the item is durably RECOVERY, but the LIVE loop itself does not raise.
            live = first.run_live_once()
            self.assertEqual(live, ["200"])
            self.assertEqual(db.get("200").state.value, "RECOVERY")
            first.close()

            # RECOVERY is the durable retry state. Startup must resume the
            # same unresolved candidate rather than abandoning it or consuming
            # a later LIVE candidate.
            restarted_source = LifecycleSource()
            restarted_source.completed = True
            restarted_source.live = []
            second = Coordinator(
                Database(storage.database / "db.sqlite"), storage,
                Vision(), Studio(storage), publisher, restarted_source,
            )
            recovered = second.recover_pending()

            self.assertEqual(recovered, ["200"])
            self.assertEqual(second.db.get("200").state.value, "PUBLISHED")
            self.assertEqual(publisher.published, ["200"])
            self.assertEqual(restarted_source.live, [])
            second.close()

    def test_run_forever_catches_up_then_processes_one_live_candidate_per_cycle(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")

            history_files = {}
            for message_id in ("100", "101", "102", "103"):
                path = root / f"{message_id}.mp4"
                path.write_bytes(message_id.encode())
                history_files[message_id] = path

            source = LifecycleSource()
            source.history = [
                SyncMessage("100", source_id="local", source_path=history_files["100"],
                            original_url="https://shopee.com.br/x/a"),
                SyncMessage("101", source_id="local", source_path=history_files["101"],
                            original_url="https://shopee.com.br/x/b"),
            ]
            source.live = [
                SyncMessage("102", source_id="local", source_path=history_files["102"],
                            original_url="https://shopee.com.br/x/c"),
                SyncMessage("103", source_id="local", source_path=history_files["103"],
                            original_url="https://shopee.com.br/x/d"),
            ]
            publisher = Publisher()
            coordinator = Coordinator(db, storage, Vision(), Studio(storage), publisher, source)

            coordinator.run_forever(max_cycles=1, poll_seconds=0)

            self.assertTrue(db.historical_complete())
            self.assertTrue(source.live_called)
            self.assertEqual(publisher.published, ["100", "101", "102"])
            self.assertEqual(
                [db.get(item_id).state.value for item_id in ("100", "101", "102")],
                ["PUBLISHED", "PUBLISHED", "PUBLISHED"],
            )
            self.assertFalse(db.conn.execute(
                "SELECT 1 FROM items WHERE content_id='103'"
            ).fetchone())
            coordinator.close()


    def test_different_telegram_ids_with_same_shopee_url_remain_distinct(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            first_path = root / "100.mp4"
            second_path = root / "101.mp4"
            first_path.write_bytes(b"first")
            second_path.write_bytes(b"second")

            from armored_core.services import SyncService
            sync = SyncService(db, storage)
            first = sync.ingest(
                first_path,
                "100",
                source_id="telegram",
                original_url="https://s.shopee.com.br/AUuG5phvYW",
            )
            second = sync.ingest(
                second_path,
                "101",
                source_id="telegram",
                original_url="https://s.shopee.com.br/AUuG5phvYW",
            )

            self.assertNotEqual(first, second)
            self.assertEqual(
                len(db.conn.execute("SELECT * FROM items").fetchall()),
                2,
            )
            self.assertEqual(db.get(first).telegram_message_id, "100")
            self.assertEqual(db.get(second).telegram_message_id, "101")
            db.close()


    def test_history_and_live_same_id_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            path = root / "100.mp4"
            path.write_bytes(b"same")

            from armored_core.services import SyncService
            sync = SyncService(db, storage)
            first = sync.ingest(path, "100", source_id="local",
                                original_url="https://shopee.com.br/x/a")
            second = sync.ingest(path, "100", source_id="local",
                                  original_url="https://shopee.com.br/x/a")
            self.assertEqual(first, second)
            self.assertEqual(len(db.conn.execute("SELECT * FROM items").fetchall()), 1)
            db.close()


    def test_shutdown_during_studio_preserves_inflight_state_for_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            source_file = root / "300.mp4"
            source_file.write_bytes(b"300")

            class InterruptingVision:
                def identify(self, item):
                    return VisionResult("affiliate", "https://example.invalid/affiliate")

            class InterruptingStudio:
                def process(self, item):
                    raise RuntimeError("rvc-child-interrupted")

            source = LifecycleSource()
            source.completed = True
            source.live = [SyncMessage(
                "300", source_id="local", source_path=source_file,
                original_url="https://shopee.com.br/x/interrupted",
            )]
            publisher = Publisher()
            coordinator = Coordinator(
                db, storage, InterruptingVision(), InterruptingStudio(), publisher, source
            )
            coordinator.pipeline.set_shutdown_checker(lambda: True)

            with self.assertRaises(KeyboardInterrupt):
                coordinator.run_live_once()

            self.assertEqual(db.get("300").state.value, "STUDIO")
            self.assertFalse(db.get("300").cleanup_completed)
            self.assertIsNone(db.publication("300"))
            coordinator.close()


class DownloadTimeoutTests(unittest.TestCase):
    def test_telegram_download_allows_slow_but_progressing_transfer(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "slow.mp4"

            class Document:
                size = 3

            class Message:
                id = 999
                document = Document()

            class SlowIterator:
                def __init__(self):
                    self.chunks = [b"a", b"b", b"c"]
                    self.index = 0

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    if self.index >= len(self.chunks):
                        raise StopAsyncIteration
                    await asyncio.sleep(0.05)
                    value = self.chunks[self.index]
                    self.index += 1
                    return value

            class Client:
                def iter_download(self, message, request_size):
                    return SlowIterator()

            class Reader:
                client = Client()

            source = TelegramSource(Path(td), Reader())
            old_idle = os.environ.get("ARMORED_SYNC_DOWNLOAD_IDLE_TIMEOUT")
            os.environ["ARMORED_SYNC_DOWNLOAD_IDLE_TIMEOUT"] = "1"
            try:
                asyncio.run(source._download_to(Message(), target))
            finally:
                if old_idle is None:
                    os.environ.pop("ARMORED_SYNC_DOWNLOAD_IDLE_TIMEOUT", None)
                else:
                    os.environ["ARMORED_SYNC_DOWNLOAD_IDLE_TIMEOUT"] = old_idle

            self.assertEqual(target.read_bytes(), b"abc")

    def test_telegram_download_stall_is_bounded_by_inactivity_timeout(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "stalled.mp4"

            class Document:
                size = 1

            class Message:
                id = 1000
                document = Document()

            class StalledIterator:
                def __aiter__(self):
                    return self

                async def __anext__(self):
                    await asyncio.sleep(2)
                    return b"x"

            class Client:
                def iter_download(self, message, request_size):
                    return StalledIterator()

            class Reader:
                client = Client()

            source = TelegramSource(Path(td), Reader())
            old_idle = os.environ.get("ARMORED_SYNC_DOWNLOAD_IDLE_TIMEOUT")
            os.environ["ARMORED_SYNC_DOWNLOAD_IDLE_TIMEOUT"] = "0"
            try:
                with self.assertRaises(TimeoutError):
                    asyncio.run(source._download_to(Message(), target))
            finally:
                if old_idle is None:
                    os.environ.pop("ARMORED_SYNC_DOWNLOAD_IDLE_TIMEOUT", None)
                else:
                    os.environ["ARMORED_SYNC_DOWNLOAD_IDLE_TIMEOUT"] = old_idle


if __name__ == "__main__":
    unittest.main()
