import os
import tempfile
import unittest
from pathlib import Path

from ArmoredHub.service import ArmoredHub
from armored_core.database import Database
from armored_core.models import PublicationCheck
from armored_core.storage import Storage


class HubContractTests(unittest.TestCase):
    def _item(self, db, storage):
        item_id = db.create_item(
            "hub-test-1",
            storage.original("hub-test-1"),
            original_url="https://shopee.example/p/1",
        )
        original = storage.original(item_id)
        original.write_bytes(b"SOURCE")
        db.conn.execute(
            "UPDATE items SET original_path=?, affiliate_name=?, affiliate_url=? WHERE content_id=?",
            (str(original), "product", "https://shopee.example/abc/finaldomeulinknovo", item_id),
        )
        db.conn.commit()
        item = db.get(item_id)
        result = storage.result(item.content_id, item.affiliate_url, item.affiliate_name)
        result.write_bytes(b"RESULT")
        db.set_result(item.item_id, result)
        return db.get(item.item_id)

    def test_hub_uses_sqlite_not_parallel_state_folder(self):
        old = os.environ.get("ARMORED_HUB_DRY_RUN")
        os.environ["ARMORED_HUB_DRY_RUN"] = "1"
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                storage = Storage(root)
                db = Database(storage.database / "db.sqlite")
                item = self._item(db, storage)
                db.publication_started(item.item_id)
                hub = ArmoredHub(root, db)
                self.assertEqual(hub.check_publication(item), PublicationCheck.UNKNOWN)

                db.conn.execute("DELETE FROM publications WHERE content_id=?", (item.item_id,))
                db.conn.commit()
                item = db.get(item.item_id)
                # Without Telegram credentials/configuration, absence cannot be
                # proven. The safe result is UNKNOWN, never ABSENT.
                self.assertEqual(hub.check_publication(item), PublicationCheck.UNKNOWN)
                with self.assertRaises(RuntimeError):
                    hub.publish(item)
                self.assertEqual(db.publication(item.item_id)["confirmed"], 0)
                self.assertEqual(db.publication(item.item_id)["verification_status"], "PENDING")
                self.assertFalse((root / "hub").exists())
                db.close()
        finally:
            if old is None:
                os.environ.pop("ARMORED_HUB_DRY_RUN", None)
            else:
                os.environ["ARMORED_HUB_DRY_RUN"] = old

    def test_unresolved_publication_reconciles_by_exact_result(self):
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                storage = Storage(root)
                db = Database(storage.database / "db.sqlite")
                item = self._item(db, storage)
                db.publication_started(item.item_id)
                hub = ArmoredHub(root, db)

                hub._find_telegram_publications = lambda current: ["9876"]
                self.assertEqual(hub.check_publication(item), PublicationCheck.CONFIRMED)
                self.assertEqual(db.publication(item.item_id)["published_message_id"], "9876")
                self.assertEqual(db.publication(item.item_id)["confirmed"], 1)
                db.close()
        finally:
            pass


    def test_unscoped_exact_publication_can_survive_missing_topic_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            try:
                item = self._item(db, storage)
                hub = ArmoredHub(root, db)

                message = type(
                    "Message",
                    (),
                    {
                        "reply_to": None,
                        "message": hub._publication_text(item),
                        "video": object(),
                        "document": None,
                    },
                )()

                self.assertFalse(
                    hub._telegram_publication_matches(
                        message, item, 228, topic_scoped=False
                    )
                )
                self.assertTrue(
                    hub._telegram_publication_matches(
                        message, item, 228,
                        topic_scoped=False,
                        allow_unknown_topic=True,
                    )
                )
            finally:
                db.close()

    def test_unscoped_candidate_is_reverified_by_message_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            try:
                item = self._item(db, storage)
                db.publication_started(item.item_id)
                hub = ArmoredHub(root, db)

                calls = []
                # Exercise the real candidate-reconciliation path. The
                # async Telegram discovery itself is replaced at the boundary,
                # but _find_telegram_publications must still re-verify the
                # returned unscoped candidate by message ID.
                hub._run_async = lambda coroutine: ["474"]
                hub._verify_telegram_message = (
                    lambda message_id, current: calls.append(message_id) or True
                )

                self.assertEqual(
                    hub.check_publication(item),
                    PublicationCheck.CONFIRMED,
                )
                self.assertEqual(calls, ["474"])
                self.assertEqual(
                    db.publication(item.item_id)["published_message_id"],
                    "474",
                )
            finally:
                db.close()

    def test_publish_once_does_not_preflight_fresh_publication(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            try:
                item = self._item(db, storage)
                hub = ArmoredHub(root, db)
                calls = {"check": 0, "publish": 0}

                def forbidden_check(current):
                    calls["check"] += 1
                    raise AssertionError("fresh publication must not run Telegram preflight")

                def fake_publish(current):
                    calls["publish"] += 1
                    return type("Result", (), {"confirmed": True, "message_id": "telegram-456"})()

                hub.check_publication = forbidden_check
                hub.publish = fake_publish

                result = hub.publish_once(item)

                self.assertTrue(result.confirmed)
                self.assertEqual(result.message_id, "telegram-456")
                self.assertEqual(calls["check"], 0)
                self.assertEqual(calls["publish"], 1)

                row = db.publication(item.item_id)
                self.assertIsNotNone(row)
                self.assertEqual(row["idempotency_key"], f"armoredcreator:content:{item.item_id}")
                self.assertEqual(row["confirmed"], 0)
            finally:
                db.close()

    def test_publish_once_uses_existing_confirmation_without_sending(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            try:
                item = self._item(db, storage)
                hub = ArmoredHub(root, db)
                db.publication_started(item.item_id)
                db.publication_message_sent(item.item_id, "telegram-123")
                db.publication_confirmed(item.item_id, "telegram-123")

                calls = {"publish": 0}
                hub.publish = lambda current: calls.__setitem__("publish", calls["publish"] + 1)
                hub.check_publication = lambda current: PublicationCheck.CONFIRMED
                result = hub.publish_once(item)

                self.assertTrue(result.confirmed)
                self.assertEqual(result.message_id, "telegram-123")
                self.assertEqual(calls["publish"], 0)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
