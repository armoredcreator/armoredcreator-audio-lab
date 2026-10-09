import tempfile
import unittest
from pathlib import Path
import hashlib

from armored_core.database import Database
from armored_core.models import PublicationCheck, State
from armored_core.pipeline import Pipeline
from armored_core.services import PublicationResult, StudioResult, SyncService, VisionResult
from armored_core.storage import Storage

class V:
    def identify(self, item):
        return VisionResult("affiliate-name", "https://example.invalid/final")

class S:
    def __init__(self, storage):
        self.storage = storage

    def process(self, item):
        w = self.storage.working(item.content_id)
        w.write_bytes(b"WORK")
        r = self.storage.result(
            item.content_id,
            item.affiliate_url,
            item.affiliate_name,
        )
        r.write_bytes(b"RESULT")
        return StudioResult(w, r)

class P:
    def __init__(self):
        self.published = set()
        self.calls = 0

    def check_publication(self, item):
        return PublicationCheck.CONFIRMED if item.item_id in self.published else PublicationCheck.ABSENT

    def publish(self, item):
        self.calls += 1
        self.published.add(item.item_id)
        return PublicationResult(True, "telegram-id")

class InvariantTests(unittest.TestCase):
    def test_original_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            st = Storage(root)
            db = Database(st.database / "db.sqlite")
            src = root / "source.mp4"
            src.write_bytes(b"IMMUTABLE")
            i = SyncService(db, st).ingest(src, "telegram-1", original_url="https://shopee.com.br/example/original")
            original = db.get(i).original_path
            Pipeline(db, st, V(), S(st), P()).run(i)
            self.assertEqual(original.read_bytes(), b"IMMUTABLE")
            self.assertEqual(list(original.parent.iterdir()), [original])
            db.close()

    def test_database_keeps_original_hash_attempts_and_recovery_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            st = Storage(root)
            db = Database(st.database / "db.sqlite")
            src = root / "source.mp4"
            payload = b"IMMUTABLE-HASH"
            src.write_bytes(payload)
            i = SyncService(db, st).ingest(src, "telegram-1")
            item = db.get(i)
            self.assertEqual(item.original_sha256, hashlib.sha256(payload).hexdigest())
            self.assertEqual(item.attempts, 0)
            self.assertEqual(item.recovery_count, 0)
            Pipeline(db, st, V(), S(st), P()).run(i)
            item = db.get(i)
            self.assertEqual(item.attempts, 1)
            self.assertEqual(item.original_sha256, hashlib.sha256(payload).hexdigest())
            db.close()

    def test_duplicate_ingest_returns_same_item(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            st = Storage(root)
            db = Database(st.database / "db.sqlite")
            src = root / "source.mp4"
            src.write_bytes(b"X")
            sync = SyncService(db, st)
            a = sync.ingest(src, "telegram-1")
            b = sync.ingest(src, "telegram-1")
            self.assertEqual(a, b)
            db.close()

    def test_telegram_style_materializer_writes_only_canonical_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            st = Storage(root)
            db = Database(st.database / "db.sqlite")
            sync = SyncService(db, st)

            def materialize(target):
                target.write_bytes(b"TELEGRAM-BYTES")

            from armored_core.services import IngestMessage
            i = sync.ingest_message(IngestMessage(
                telegram_message_id="1383",
                source_id="telegram",
                original_url="https://shopee.com.br/example",
                materialize=materialize,
            ))
            item = db.get(i)
            self.assertEqual(
                item.original_path,
                st.original(i, original_url="https://shopee.com.br/example"),
            )
            self.assertTrue(item.original_path.is_file())
            self.assertEqual(item.original_path.read_bytes(), b"TELEGRAM-BYTES")
            self.assertEqual(item.original_sha256, hashlib.sha256(b"TELEGRAM-BYTES").hexdigest())
            self.assertFalse((root / "storage" / "sync").exists())
            self.assertEqual([p.name for p in item.workspace.iterdir()], ["1383_finallinkoriginal.mp4"])
            db.close()

    def test_missing_original_blocks_processing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            st = Storage(root)
            db = Database(st.database / "db.sqlite")
            src = root / "source.mp4"
            src.write_bytes(b"X")
            i = SyncService(db, st).ingest(src, "telegram-1")
            original = db.get(i).original_path
            original.unlink()
            with self.assertRaises(FileNotFoundError):
                Pipeline(db, st, V(), S(st), P()).run(i)
            self.assertEqual(db.get(i).state, State.RECEIVED)
            self.assertIn("immutable-original-missing", db.last_error(i))
            db.close()

if __name__ == "__main__":
    unittest.main()
