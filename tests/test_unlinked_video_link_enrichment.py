import tempfile
import unittest
from pathlib import Path

from armored_core.database import Database
from armored_core.models import State
from armored_core.services import IngestMessage, SyncService
from armored_core.storage import Storage


class UnlinkedVideoEnrichmentTests(unittest.TestCase):
    def test_later_shopee_link_reopens_waiting_vision_without_downloading(self):
        with tempfile.TemporaryDirectory() as td:
            storage = Storage(Path(td))
            db = Database(storage.database / "db.sqlite")
            sync = SyncService(db, storage)

            def forbidden_download(_target):
                raise AssertionError("reserve_message must never download media")

            item_id = sync.reserve_message(
                IngestMessage(
                    telegram_message_id="501",
                    source_id="source1",
                    original_url=None,
                    materialize=forbidden_download,
                )
            )
            db.mark_vision_waiting(item_id, "link ausente")
            self.assertEqual(db.get(item_id).state, State.WAITING_VISION)

            same_item_id = sync.reserve_message(
                IngestMessage(
                    telegram_message_id="501",
                    source_id="source1",
                    original_url="https://shopee.com.br/product/1",
                    materialize=forbidden_download,
                )
            )

            item = db.get(item_id)
            self.assertEqual(same_item_id, item_id)
            self.assertEqual(item.original_url, "https://shopee.com.br/product/1")
            self.assertEqual(item.state, State.RECEIVED)
            self.assertFalse(item.original_path.is_file())
            db.close()


if __name__ == "__main__":
    unittest.main()
