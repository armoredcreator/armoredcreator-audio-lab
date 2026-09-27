from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from armored_core.coordinator import Coordinator
from armored_core.database import Database
from armored_core.storage import Storage


class FailedItemStartupTests(unittest.TestCase):
    def test_legacy_failed_item_is_reopened_for_recovery_on_startup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            original = storage.original("telegram-530", original_url="https://shopee.com.br/p/missing")
            original.write_bytes(b"ORIGINAL")

            db = Database(storage.database / "armoredcreator.db")
            item_id = db.create_item(
                "telegram-530",
                original,
                topic_id=228,
                topic_name="Telegram 228",
                original_url="https://shopee.com.br/p/missing",
            )
            db.fail(item_id, "ShopeeAPIError: Produto não encontrado")
            coordinator = Coordinator(db, storage, None, None, None, None)

            attempted = []
            coordinator.recover = lambda value: attempted.append(value)

            recovered = coordinator.recover_pending()

            self.assertEqual(recovered, [item_id])
            self.assertEqual(attempted, [item_id])
            self.assertEqual(db.get(item_id).state.value, "RECOVERY")
            db.close()


if __name__ == "__main__":
    unittest.main()
