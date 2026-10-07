import tempfile
import unittest
from pathlib import Path

from armored_core.database import Database
from armored_core.services import IngestMessage, SyncService
from armored_core.storage import Storage


class LegacyIngestRecoveryTests(unittest.TestCase):
    def test_existing_blank_original_path_is_repaired_and_materialized(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            db.reserve_item(
                "544",
                source_id="telegram",
                original_url="https://s.shopee.com.br/5AqOWAutYS",
                original_path=Path(""),
            )

            stale = storage.workspace("544", source_id="telegram") / "544_5AqOWAutYS.mp4.part"
            stale.write_bytes(b"stale-partial")

            calls = []
            async def materialize(target):
                calls.append(target)
                target.write_bytes(b"fresh-original")

            sync = SyncService(db, storage)
            item_id = __import__("asyncio").run(
                sync.ingest_message_async(
                    IngestMessage(
                        telegram_message_id="544",
                        original_url="https://s.shopee.com.br/5AqOWAutYS",
                        materialize=materialize,
                    )
                )
            )

            original = storage.original("544", ".mp4", "https://s.shopee.com.br/5AqOWAutYS")
            self.assertEqual(item_id, "544")
            self.assertEqual(db.get("544").original_path, original)
            self.assertTrue(original.is_file())
            self.assertEqual(original.read_bytes(), b"fresh-original")
            self.assertFalse(stale.exists())
            self.assertEqual(len(calls), 1)
            db.close()

    def test_existing_valid_original_is_not_redownloaded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            original = storage.original("100", ".mp4", "https://s.shopee.com.br/abc")
            original.write_bytes(b"already-final")
            db.reserve_item(
                "100",
                original_url="https://s.shopee.com.br/abc",
                original_path=original,
            )

            async def must_not_run(target):
                raise AssertionError("materializer should not run for an existing original")

            sync = SyncService(db, storage)
            item_id = __import__("asyncio").run(
                sync.ingest_message_async(
                    IngestMessage(
                        telegram_message_id="100",
                        original_url="https://s.shopee.com.br/abc",
                        materialize=must_not_run,
                    )
                )
            )

            self.assertEqual(item_id, "100")
            self.assertEqual(original.read_bytes(), b"already-final")
            db.close()

    def test_repaired_path_never_becomes_windows_path_dot(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            db.reserve_item(
                "200",
                original_url="https://s.shopee.com.br/test",
                original_path=Path(""),
            )

            sync = SyncService(db, storage)
            expected = storage.original("200", ".mp4", "https://s.shopee.com.br/test")

            async def materialize(target):
                target.write_bytes(b"x")

            __import__("asyncio").run(
                sync.ingest_message_async(
                    IngestMessage(
                        telegram_message_id="200",
                        original_url="https://s.shopee.com.br/test",
                        materialize=materialize,
                    )
                )
            )

            repaired = db.get("200").original_path
            self.assertNotEqual(repaired, Path("."))
            self.assertEqual(repaired, expected)
            db.close()


if __name__ == "__main__":
    unittest.main()
