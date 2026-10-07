import os
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
        self.calls = 0

    def process(self, item):
        self.calls += 1
        w = self.storage.working(item.item_id)
        w.write_bytes(item.original_path.read_bytes())
        r = self.storage.result(item.item_id, item.affiliate_name or "recover-final")
        r.write_bytes(w.read_bytes())
        return StudioResult(w, r)


class CrashStudio(Studio):
    def process(self, item):
        self.calls += 1
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
        self.always_absent = False

    def check_publication(self, item):
        if self.always_absent:
            return PublicationCheck.ABSENT
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

    def test_ia_failure_recovers_without_rerunning_vision_and_keeps_context(self):
        class CountingVision:
            def __init__(self):
                self.calls = 0
                self.context = {
                    "product_name": "Produto teste",
                    "affiliate_links": ["https://shopee.com.br/item"],
                    "source": "vision-v1",
                }

            def identify(self, item):
                self.calls += 1
                return VisionResult(
                    "recover-final",
                    "https://example.invalid/a",
                    ia_context=self.context,
                )

        class FailOnceIA:
            def __init__(self):
                self.calls = 0

            def generate_caption(self, product_context):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("simulated-ia-crash")
                self.context_seen = dict(product_context)
                return "Oferta boa 😊"

        vision = CountingVision()
        ia = FailOnceIA()
        studio = Studio(self.storage)
        publisher = Publisher()

        first_pipeline = Pipeline(
            self.db,
            self.storage,
            vision,
            studio,
            publisher,
            ia=ia,
        )

        previous_ia = os.environ.get("ARMORED_IA_ENABLED")
        previous_caption = os.environ.get("ARMORED_IA_CAPTION_ENABLED")
        os.environ["ARMORED_IA_ENABLED"] = "1"
        os.environ["ARMORED_IA_CAPTION_ENABLED"] = "1"
        try:
            with self.assertRaises(RuntimeError):
                first_pipeline.run(self.item)

            after_failure = self.db.get(self.item)
            self.assertEqual(after_failure.state, State.RECOVERY)
            self.assertEqual(vision.calls, 1)
            self.assertEqual(after_failure.ia_context, vision.context)
            self.assertIsNone(after_failure.publication_caption)

            # Simulate process restart: construct a new Recovery coordinator
            # around the same durable SQLite state and the same one-shot IA.
            Recovery(
                self.db,
                self.storage,
                vision,
                Studio(self.storage),
                publisher,
                ia=ia,
            ).reconcile(self.item)

            recovered = self.db.get(self.item)
            self.assertEqual(recovered.state, State.PUBLISHED)
            self.assertEqual(vision.calls, 1)
            self.assertEqual(ia.calls, 2)
            self.assertEqual(ia.context_seen, vision.context)
            self.assertEqual(recovered.publication_caption, "Oferta boa 😊")
            self.assertEqual(publisher.count, 1)
        finally:
            if previous_ia is None:
                os.environ.pop("ARMORED_IA_ENABLED", None)
            else:
                os.environ["ARMORED_IA_ENABLED"] = previous_ia
            if previous_caption is None:
                os.environ.pop("ARMORED_IA_CAPTION_ENABLED", None)
            else:
                os.environ["ARMORED_IA_CAPTION_ENABLED"] = previous_caption

    def test_recovery_retries_waiting_vision(self):
        vision = VisionWaitThenResolve()
        pipeline = Pipeline(self.db, self.storage, vision, Studio(self.storage), self.pub)

        pipeline.run(self.item)
        self.assertEqual(self.db.get(self.item).state, State.WAITING_VISION)

        Recovery(self.db, self.storage, vision, Studio(self.storage), self.pub).reconcile(self.item)

        row = self.db.get(self.item)
        self.assertEqual(row.state, State.PUBLISHED)
        self.assertEqual(vision.calls, 2)
        self.assertEqual(self.pub.count, 1)

    def test_rebuilds_after_studio_crash_from_original_not_partial_artifacts(self):
        studio = CrashStudio(self.storage)
        with self.assertRaises(RuntimeError):
            Pipeline(self.db, self.storage, Vision(), studio, self.pub).run(self.item)

        row = self.db.get(self.item)
        self.assertEqual(row.state, State.RECOVERY)

        # Simulate a crashed RVC/Studio leaving corrupt derived artifacts.
        working = self.storage.working(self.item)
        result = self.storage.result(self.item, "recover-final")
        working.write_bytes(b"PARTIAL-WORKING")
        result.write_bytes(b"PARTIAL-RESULT")
        self.db.set_working(self.item, working)
        self.db.set_result(self.item, result)

        clean_studio = Studio(self.storage)
        Recovery(
            self.db, self.storage, Vision(), clean_studio, self.pub
        ).reconcile(self.item)

        row = self.db.get(self.item)
        self.assertEqual(row.state, State.PUBLISHED)
        self.assertEqual(clean_studio.calls, 1)
        self.assertEqual(row.original_path.read_bytes(), b"VIDEO")
        # Successful publication performs the normal cleanup; only the immutable original remains.\n        self.assertFalse(row.result_path.exists())\n        self.assertEqual([p.name for p in row.workspace.iterdir()], [row.original_path.name])
        self.assertEqual(self.pub.count, 1)

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
        self.db.transition(self.item, State.STUDIO, "test-result-missing")
        result = self.storage.result(self.item, row.affiliate_name or "recover-final")
        result.unlink(missing_ok=True)
        self.db.set_result(self.item, result)
        Recovery(self.db, self.storage, Vision(), Studio(self.storage), self.pub).reconcile(self.item)
        self.assertEqual(self.db.get(self.item).state, State.PUBLISHED)
        self.assertEqual(self.pub.count, 1)

    def test_absent_publication_rebuilds_from_original_even_with_durable_result(self):
        vision = Vision()
        studio = Studio(self.storage)
        publisher = Publisher()
        pipeline = Pipeline(self.db, self.storage, vision, studio, publisher)

        pipeline.run(self.item)
        row = self.db.get(self.item)
        self.assertEqual(row.state, State.PUBLISHED)

        # Simulate a prior publication attempt whose Telegram message has no
        # matching evidence. A durable result may still exist, but it is not
        # trusted as a safe continuation point.
        self.db.transition(self.item, State.RECOVERY, "test-publication-absent")
        self.db.publication_started(self.item)
        self.db.publication_send_started(self.item)
        self.db.publication_message_sent(self.item, "stale-message")
        working = self.storage.working(self.item)
        working.write_bytes(b"STALE-WORKING")
        self.db.set_working(self.item, working)

        # The real Telegram verifier will report ABSENT after all searches
        # complete with zero exact matches. Model that boundary explicitly;
        # the initial successful publication must not make this synthetic
        # recovery test appear CONFIRMED.
        publisher.always_absent = True
        studio.calls = 0
        Recovery(
            self.db, self.storage, vision, studio, publisher
        ).reconcile(self.item)

        row = self.db.get(self.item)
        self.assertEqual(row.state, State.PUBLISHED)
        self.assertEqual(studio.calls, 1)
        self.assertEqual(row.original_path.read_bytes(), b"VIDEO")
        self.assertFalse(row.result_path.exists())
        self.assertEqual([p.name for p in row.workspace.iterdir()], [row.original_path.name])

    def test_unknown_publication_never_resumes_durable_result(self):
        class AmbiguousPublisher(Publisher):
            def check_publication(self, item):
                return PublicationCheck.UNKNOWN

        publisher = AmbiguousPublisher()
        vision = Vision()
        studio = Studio(self.storage)

        pipeline = Pipeline(self.db, self.storage, vision, studio, publisher)
        pipeline.run(self.item)
        row = self.db.get(self.item)
        self.assertEqual(row.state, State.RECOVERY)
        self.assertTrue(row.result_path.is_file())
        self.assertEqual(publisher.count, 0)

        with self.assertRaisesRegex(RuntimeError, "publication-check-uncertain-recovery-stopped"):
            Recovery(self.db, self.storage, vision, studio, publisher).reconcile(self.item)

        self.assertEqual(self.db.get(self.item).state, State.RECOVERY)
        self.assertEqual(publisher.count, 0)

    def test_cleanup_is_idempotent(self):
        Pipeline(self.db, self.storage, Vision(), Studio(self.storage), self.pub).run(self.item)
        Recovery(self.db, self.storage, Vision(), Studio(self.storage), self.pub).reconcile(self.item)
        self.assertTrue(self.db.get(self.item).original_path.exists())


    def test_source2_recovery_rebuilds_derived_files_in_source2_workspace(self):
        from types import SimpleNamespace

        source_id = "-1002698134896"
        from unittest.mock import patch

        patcher = patch.dict(
            "os.environ",
            {
                "ARMORED_SOURCE_2_ID": source_id,
                "ARMORED_SOURCE_2_CHAT_ID": source_id,
                "ARMORED_SOURCE_2_VIDEO_DIR": "Videos GRUPO_FONTE_2",
            },
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        item_id = self.db.content_id_for("77", source_id)
        original = self.storage.original(
            item_id,
            original_url="https://shopee.com.br/77",
            source_id=source_id,
        )
        original.write_bytes(b"SOURCE2-ORIGINAL")
        item_id = self.db.create_item(
            "77",
            original,
            source_id=source_id,
            topic_id=1160,
            topic_name="source2",
            original_url="https://shopee.com.br/77",
        )
        self.db.set_vision(item_id, "recover-final", "https://example.invalid/a")
        self.db.transition(item_id, State.RECOVERY, "source2-test")

        class Source2Studio:
            def __init__(self, storage):
                self.storage = storage
                self.calls = 0

            def process(self, item):
                self.calls += 1
                working = self.storage.working(
                    item.content_id,
                    source_id=item.source_id,
                )
                result = self.storage.result(
                    item.content_id,
                    item.affiliate_url,
                    item.affiliate_name,
                    source_id=item.source_id,
                )
                payload = item.original_path.read_bytes()
                working.write_bytes(payload + b"-W")
                result.write_bytes(payload + b"-R")
                return StudioResult(working, result)

        studio = Source2Studio(self.storage)
        Recovery(
            self.db,
            self.storage,
            Vision(),
            studio,
            self.pub,
        ).reconcile(item_id)

        row = self.db.get(item_id)
        self.assertEqual(row.state, State.PUBLISHED)
        self.assertEqual(studio.calls, 1)
        expected_workspace = (
            self.storage.storage / "Videos GRUPO_FONTE_2" / item_id
        )
        self.assertEqual(row.workspace, expected_workspace)
        self.assertEqual(
            row.original_path,
            expected_workspace / f"{item_id}_77.mp4",
        )
        self.assertFalse(row.result_path.exists())
        self.assertEqual(
            [p.name for p in expected_workspace.iterdir()],
            [row.original_path.name],
        )


if __name__ == "__main__":
    unittest.main()
