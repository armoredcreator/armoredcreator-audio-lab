import tempfile
import unittest
from pathlib import Path

from armored_core.database import Database
from armored_core.models import PublicationCheck, State
from armored_core.pipeline import Pipeline
from armored_core.services import PublicationResult, PublicationUnknownError, StudioResult, SyncService, VisionResult
from armored_core.storage import Storage

class Vision:
    def identify(self, item):
        return VisionResult("produto-final", "https://example.invalid/a")

class Studio:
    def __init__(self, storage):
        self.storage = storage

    def process(self, item):
        w = self.storage.working(item.item_id)
        w.write_bytes(item.original_path.read_bytes())
        r = self.storage.result(item.item_id, item.affiliate_name)
        r.write_bytes(w.read_bytes())
        return StudioResult(w, r)

class Publisher:
    def __init__(self):
        self.count = 0
        self.ids = set()

    def check_publication(self, item):
        return PublicationCheck.CONFIRMED if item.item_id in self.ids else PublicationCheck.ABSENT

    def publish(self, item):
        self.count += 1
        self.ids.add(item.item_id)
        return PublicationResult(True, str(self.count))

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        root = Path(self.td.name)
        self.storage = Storage(root)
        self.db = Database(self.storage.database / "armoredcreator.db")
        src = root / "source.mp4"
        src.write_bytes(b"VIDEO")
        self.item = SyncService(self.db, self.storage).ingest(src, "msg-1")
        self.pub = Publisher()

    def tearDown(self):
        self.db.close()
        self.td.cleanup()

    def test_happy_path_keeps_only_original(self):
        Pipeline(self.db, self.storage, Vision(), Studio(self.storage), self.pub).run(self.item)
        row = self.db.get(self.item)
        self.assertEqual(row.state, State.PUBLISHED)
        self.assertEqual(row.original_path.read_bytes(), b"VIDEO")
        self.assertEqual([p.name for p in row.workspace.iterdir()], [row.original_path.name])
        self.assertEqual(self.pub.count, 1)
        self.assertTrue(row.working_path is not None)
        self.assertTrue(row.result_path is not None)

    def test_publish_once_receives_fresh_publication_without_preflight(self):
        class IdempotentPublisher:
            def __init__(self, db):
                self.db = db
                self.calls = 0

            def publish_once(self, item):
                self.calls += 1
                if self.db.publication(item.item_id) is not None:
                    raise AssertionError("Pipeline must not pre-create publication before publish_once")
                return PublicationResult(True, "telegram-456")

        pub = IdempotentPublisher(self.db)
        p = Pipeline(self.db, self.storage, Vision(), Studio(self.storage), pub)
        p.run(self.item)

        self.assertEqual(pub.calls, 1)
        self.assertEqual(self.db.get(self.item).state, State.PUBLISHED)


    def test_armored_ia_runs_between_vision_and_studio(self):
        class IA:
            def __init__(self):
                self.calls = 0
            def generate_caption(self, context):
                self.calls += 1
                assert context["productName"] == "produto-final"
                return "Olha esse charme ✨\n#casa"

        ia = IA()
        import os
        old_enabled = os.environ.get("ARMORED_IA_ENABLED")
        old_caption = os.environ.get("ARMORED_IA_CAPTION_ENABLED")
        os.environ["ARMORED_IA_ENABLED"] = "1"
        os.environ["ARMORED_IA_CAPTION_ENABLED"] = "1"

        class VisionWithContext:
            def identify(self, item):
                return VisionResult(
                    "produto-final",
                    "https://example.invalid/a",
                    ia_context={"productName": "produto-final"},
                )

        try:
            p = Pipeline(
                self.db,
                self.storage,
                VisionWithContext(),
                Studio(self.storage),
                self.pub,
                ia,
            )
            p.run(self.item)
            row = self.db.get(self.item)
            self.assertEqual(row.state, State.PUBLISHED)
            self.assertEqual(row.publication_caption, "Olha esse charme ✨\n#casa")
            self.assertEqual(ia.calls, 1)
        finally:
            if old_enabled is None:
                os.environ.pop("ARMORED_IA_ENABLED", None)
            else:
                os.environ["ARMORED_IA_ENABLED"] = old_enabled
            if old_caption is None:
                os.environ.pop("ARMORED_IA_CAPTION_ENABLED", None)
            else:
                os.environ["ARMORED_IA_CAPTION_ENABLED"] = old_caption

    def test_telegram_timeout_enters_recovery_without_becoming_failed(self):
        class UnknownPublisher(Publisher):
            def publish(self, item):
                self.count += 1
                raise PublicationUnknownError("Telegram publication outcome is UNKNOWN")

        pub = UnknownPublisher()
        p = Pipeline(self.db, self.storage, Vision(), Studio(self.storage), pub)

        p.run(self.item)

        row = self.db.get(self.item)
        self.assertEqual(row.state, State.RECOVERY)
        self.assertNotEqual(row.state, State.FAILED)
        self.assertEqual(pub.count, 1)
        publication = self.db.publication(self.item)
        self.assertIsNotNone(publication)
        self.assertEqual(publication["verification_status"], "PENDING")

    def test_second_run_does_not_republish(self):
        p = Pipeline(self.db, self.storage, Vision(), Studio(self.storage), self.pub)
        p.run(self.item)
        p.run(self.item)
        self.assertEqual(self.pub.count, 1)

if __name__ == "__main__":
    unittest.main()
