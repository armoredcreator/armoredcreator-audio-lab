import tempfile
import unittest
from pathlib import Path

from armored_core.database import Database
from armored_core.models import PublicationCheck, State
from armored_core.pipeline import Pipeline
from armored_core.recovery import Recovery
from armored_core.services import PublicationResult, StudioResult, SyncService, VisionResult
from armored_core.storage import Storage

class Vision:
    def identify(self, item):
        return VisionResult("recover-final", "https://example.invalid/a")

class Studio:
    def __init__(self, storage):
        self.storage = storage

    def process(self, item):
        w = self.storage.working(item.item_id)
        w.write_bytes(item.original_path.read_bytes())
        r = self.storage.result(item.item_id, item.affiliate_name or "recover-final")
        r.write_bytes(w.read_bytes())
        return StudioResult(w, r)

class CrashStudio(Studio):
    def process(self, item):
        raise RuntimeError("simulated studio crash")


class VisionWaitThenResolve:
    def __init__(self):
        self.calls = 0

    def identify(self, item):
        self.calls += 1
        if self.calls == 1:
            from armored_core.services import VisionUnresolvedError
            raise VisionUnresolvedError("simulated unresolved vision")
        return VisionResult("recover-final", "https://example.invalid/a")

class Publisher:
    def __init__(self):
        self.ids = set()
        self.count = 0

    def check_publication(self, item):
        return PublicationCheck.CONFIRMED if item.item_id in self.ids else PublicationCheck.ABSENT

    def publish(self, item):
        self.count += 1
        self.ids.add(item.item_id)
        return PublicationResult(True, str(self.count))

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        root = Path(self.td.name)
        self.storage = Storage(root)
        self.db = Database(self.storage.database / "armoredcreator.db")
        src = root / "source.mp4"
        src.write_bytes(b"VIDEO")
        self.item = SyncService(self.db, self.storage).ingest(src, "msg")
        self.pub = Publisher()

    def tearDown(self):
        self.db.close()
        self.td.cleanup()

    def test_recovery_retries_waiting_vision(self):
        vision = VisionWaitThenResolve()
        pipeline = Pipeline(self.db, self.storage, vision, Studio(self.storage), self.pub)

        # First Vision attempt is unresolved and must become a durable
        # WAITING_VISION state rather than being treated as completed.
        pipeline.run(self.item)
        self.assertEqual(self.db.get(self.item).state, State.WAITING_VISION)

        # Recovery owns the retry. It must re-enter VISION and continue the
        # same item through Studio and publication when Vision resolves.
        Recovery(self.db, self.storage, vision, Studio(self.storage), self.pub).reconcile(self.item)

        row = self.db.get(self.item)
        self.assertEqual(row.state, State.PUBLISHED)
        self.assertEqual(vision.calls, 2)
        self.assertEqual(self.pub.count, 1)

    def test_rebuilds_after_studio_crash(self):
        with self.assertRaises(RuntimeError):
            Pipeline(self.db, self.storage, Vision(), CrashStudio(self.storage), self.pub).run(self.item)
        self.assertEqual(self.db.get(self.item).state, State.RECOVERY)
        Recovery(self.db, self.storage, Vision(), Studio(self.storage), self.pub).reconcile(self.item)
        row = self.db.get(self.item)
        self.assertEqual(row.state, State.PUBLISHED)
        self.assertTrue(row.original_path.exists())
        self.assertEqual([p.name for p in row.workspace.iterdir()], [row.original_path.name])

    def test_legacy_failed_item_is_reopened_into_recovery(self):
        pipeline = Pipeline(self.db, self.storage, Vision(), Studio(self.storage), self.pub)
        pipeline.run(self.item)
        self.db.fail(self.item, "legacy failure before universal recovery")
        self.assertEqual(self.db.get(self.item).state, State.FAILED)

        Recovery(self.db, self.storage, Vision(), Studio(self.storage), self.pub).reconcile(self.item)

        self.assertEqual(self.db.get(self.item).state, State.PUBLISHED)
        self.assertEqual(self.pub.count, 1)

    def test_recovery_rebuilds_working_when_result_missing(self):
        Pipeline(self.db, self.storage, Vision(), Studio(self.storage), self.pub).run(self.item)
        row = self.db.get(self.item)
        # Simulate a crash before publication after the result disappears.
        self.db.transition(self.item, State.STUDIO, "test-result-missing")
        result = self.storage.result(self.item, row.affiliate_name or "recover-final")
        result.unlink(missing_ok=True)
        self.db.set_result(self.item, result)
        Recovery(self.db, self.storage, Vision(), Studio(self.storage), self.pub).reconcile(self.item)
        self.assertEqual(self.db.get(self.item).state, State.PUBLISHED)
        self.assertEqual(self.pub.count, 1)

    def test_unknown_publication_never_resumes_durable_result(self):
        class AmbiguousPublisher(Publisher):
            def check_publication(self, item):
                return PublicationCheck.UNKNOWN

        publisher = AmbiguousPublisher()
        vision = Vision()
        studio = Studio(self.storage)

        # First pass reaches publication and becomes RECOVERY after an
        # ambiguous Telegram outcome, while the durable result remains.
        pipeline = Pipeline(self.db, self.storage, vision, studio, publisher)
        pipeline.run(self.item)
        row = self.db.get(self.item)
        self.assertEqual(row.state, State.RECOVERY)
        self.assertTrue(row.result_path.is_file())
        self.assertEqual(publisher.count, 0)

        # Recovery must stop on UNKNOWN. It must never fall through to the
        # durable-result path and send the same result a second time.
        with self.assertRaisesRegex(RuntimeError, "publication-check-uncertain-recovery-stopped"):
            Recovery(self.db, self.storage, vision, studio, publisher).reconcile(self.item)

        self.assertEqual(self.db.get(self.item).state, State.RECOVERY)
        self.assertEqual(publisher.count, 0)

    def test_cleanup_is_idempotent(self):
        Pipeline(self.db, self.storage, Vision(), Studio(self.storage), self.pub).run(self.item)
        Recovery(self.db, self.storage, Vision(), Studio(self.storage), self.pub).reconcile(self.item)
        self.assertTrue(self.db.get(self.item).original_path.exists())

if __name__ == "__main__":
    unittest.main()
