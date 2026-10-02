import tempfile
import unittest
from pathlib import Path

from armored_core.database import Database
from armored_core.models import State
from armored_core.startup_audit import StartupReconciler
from armored_core.storage import Storage


class _Publisher:
    def __init__(self):
        self.checked = []

    def check_publication(self, item):
        self.checked.append(item.content_id)
        raise AssertionError("confirmed publication should not be queried")


class _AbsentPublisher:
    def __init__(self):
        self.checked = []

    def check_publication(self, item):
        self.checked.append(item.content_id)
        return "ABSENT"


class _ReconcilerPublisher:
    def __init__(self, db):
        self.db = db
        self.checked = []

    def check_publication(self, item):
        self.checked.append(item.content_id)
        self.db.publication_confirmed(item.content_id, "823")
        return "CONFIRMED"


class StartupAuditTests(unittest.TestCase):
    def test_startup_audit_reports_inventory_and_does_not_touch_confirmed_publication(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            original = storage.original("101", original_url="https://shopee.com.br/101")
            original.write_bytes(b"ORIGINAL")

            db = Database(storage.database / "armoredcreator.db")
            item_id = db.create_item(
                "101",
                original,
                topic_id=228,
                topic_name="topic",
                original_url="https://shopee.com.br/101",
            )
            db.set_result(item_id, storage.result("101", "https://shopee.com.br/101", "produto"))
            db.publication_started(item_id, destination_chat_id="-100", destination_topic_id=228)
            db.publication_confirmed(item_id, "900")
            db.transition(item_id, State.PUBLISHED, "test")
            db.mark_cleanup_completed(item_id)

            orphan = storage.videos / "orphan-999"
            orphan.mkdir(parents=True)

            publisher = _Publisher()
            summary = StartupReconciler(db, storage, publisher).run()

            self.assertEqual(summary["items"], 1)
            self.assertEqual(summary["published"], 1)
            self.assertEqual(summary["publication_confirmed"], 1)
            self.assertEqual(summary["orphans"], ["orphan-999"])
            self.assertEqual(publisher.checked, [])

            db.close()

    def test_startup_audit_recovers_failed_absent_item_with_durable_result(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            original = storage.original("823", original_url="https://shopee.com.br/823")
            original.write_bytes(b"ORIGINAL")

            db = Database(storage.database / "armoredcreator.db")
            item_id = db.create_item(
                "823",
                original,
                topic_id=228,
                topic_name="topic",
                original_url="https://shopee.com.br/823",
            )
            result = storage.result("823", "https://shopee.com.br/823", "produto")
            result.write_bytes(b"FINAL-RESULT")
            db.set_vision(item_id, "produto", "https://shopee.com.br/823")
            db.set_result(item_id, result)
            db.publication_started(item_id, destination_chat_id="-100", destination_topic_id=228)
            db.fail(item_id, "RuntimeError: ARMORED_HUB_DRY_RUN=1: publicação real bloqueada")

            publisher = _AbsentPublisher()
            summary = StartupReconciler(db, storage, publisher).run()

            self.assertEqual(publisher.checked, ["823"])
            self.assertEqual(db.get(item_id).state, State.RECOVERY)
            self.assertEqual(summary["failed"], 0)
            self.assertEqual(summary["pending"], 1)
            self.assertEqual(summary["publication_ambiguous"], 1)
            self.assertTrue(result.is_file())

            db.close()

    def test_startup_migrates_legacy_caption_waiting_to_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            original = storage.original("412", original_url="https://shopee.com.br/412")
            original.write_bytes(b"ORIGINAL")

            db = Database(storage.database / "armoredcreator.db")
            item_id = db.create_item("412", original, original_url="https://shopee.com.br/412")
            db.mark_vision_waiting(
                item_id,
                "Gemini não conseguiu gerar uma legenda válida após 5 tentativa(s)",
            )

            summary = StartupReconciler(db, storage).run()

            self.assertEqual(db.get(item_id).state, State.RECOVERY)
            event = db.last_state_event(item_id)
            self.assertEqual(event["new_state"], State.RECOVERY.value)
            self.assertEqual(
                event["reason"],
                "legacy-caption-waiting-vision-migrated-to-recovery",
            )
            self.assertEqual(summary["pending"], 1)
            db.close()

    def test_startup_audit_refreshes_publication_state_after_reconciliation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            original = storage.original("542", original_url="https://shopee.com.br/542")
            original.write_bytes(b"ORIGINAL")

            db = Database(storage.database / "armoredcreator.db")
            item_id = db.create_item(
                "542",
                original,
                topic_id=228,
                topic_name="topic",
                original_url="https://shopee.com.br/542",
            )
            db.set_result(item_id, storage.result("542", "https://shopee.com.br/542", "produto"))
            db.publication_started(item_id, destination_chat_id="-100", destination_topic_id=228)

            publisher = _ReconcilerPublisher(db)
            summary = StartupReconciler(db, storage, publisher).run()

            publication = db.publication(item_id)
            self.assertEqual(publisher.checked, ["542"])
            self.assertEqual(publication["published_message_id"], "823")
            self.assertEqual(publication["confirmed"], 1)
            self.assertEqual(summary["publication_confirmed"], 1)
            self.assertEqual(summary["publication_ambiguous"], 0)

            db.close()


if __name__ == "__main__":
    unittest.main()
