import os
import tempfile
import unittest
from pathlib import Path

from armored_core.coordinator import Coordinator
from armored_core.models import PublicationCheck, State
from armored_core.services import PublicationResult, PublicationUnknownError, VisionResult
from ArmoredStudio.service import ArmoredStudio

class Source:
    def __init__(self, path):
        self.path = path
        self.used = False
    def fetch_next(self):
        if self.used:
            return None
        self.used = True
        from armored_core.production_contracts import SourceMessage
        return SourceMessage(self.path, "e2e-100", "local", original_url="https://example.invalid/product")

class Vision:
    def identify(self, item):
        return VisionResult("e2e-product", "https://example.invalid/affiliate")

class CrashStudio:
    def __init__(self, real): self.real = real
    def process(self, item): raise RuntimeError("simulated studio crash")

class PersistentPublisher:
    def __init__(self, published=None, crash_after_publish=False, db=None):
        self.published = published if published is not None else set()
        self.count = 0
        self.crash_after_publish = crash_after_publish
        self.db = db

    def check_publication(self, item):
        if item.item_id in self.published:
            if self.db is not None:
                self.db.publication_message_sent(item.item_id, f"e2e-message-{item.item_id}")
            return PublicationCheck.CONFIRMED
        return PublicationCheck.ABSENT

    def publish(self, item):
        self.count += 1
        self.published.add(item.item_id)
        if self.db is not None:
            self.db.publication_message_sent(item.item_id, f"e2e-message-{item.item_id}")
        if self.crash_after_publish:
            raise PublicationUnknownError("simulated crash after external publication")
        return PublicationResult(True, f"e2e-message-{item.item_id}")

class Bindings:
    def __init__(self, source, vision, studio, publisher):
        self.source, self.vision, self.studio, self.publisher = source, vision, studio, publisher

class EndToEndRecoveryTests(unittest.TestCase):
    def test_real_studio_chain_recovers_after_studio_failure(self):
        os.environ["ARMORED_STUDIO_ALLOW_COPY"]="1"
        os.environ["ARMORED_STUDIO_FORCE_COPY"]="1"
        try:
            with tempfile.TemporaryDirectory() as td:
                root=Path(td); input_dir=root/"input"; input_dir.mkdir()
                source=input_dir/"e2e-200.mp4"; original=b"DETERMINISTIC-E2E-VIDEO"; source.write_bytes(original)
                publisher=PersistentPublisher()
                coordinator=Coordinator.build(root, Bindings(Source(source),Vision(),CrashStudio(ArmoredStudio(root)),publisher))
                item_id=coordinator.ingest_once()
                with self.assertRaises(RuntimeError): coordinator.run(item_id)
                self.assertEqual(coordinator.db.get(item_id).state,State.RECOVERY)
                coordinator.close()
                coordinator=Coordinator.build(root, Bindings(Source(source),Vision(),ArmoredStudio(root),publisher))
                coordinator.recover(item_id)
                row=coordinator.db.get(item_id)
                self.assertEqual(row.state,State.PUBLISHED)
                self.assertEqual(row.original_path.read_bytes(),original)
                self.assertEqual(publisher.count,1)
                self.assertEqual([p.name for p in row.workspace.iterdir()],[row.original_path.name])
                events=[r["new_state"] for r in coordinator.db.conn.execute("SELECT new_state FROM state_events WHERE content_id=? ORDER BY id",(item_id,)).fetchall()]
                self.assertIn(State.VISION.value,events); self.assertIn(State.STUDIO.value,events)
                self.assertIn(State.PUBLISHING.value,events); self.assertIn(State.RECOVERY.value,events)
                coordinator.close()
        finally:
            os.environ.pop("ARMORED_STUDIO_ALLOW_COPY",None); os.environ.pop("ARMORED_STUDIO_FORCE_COPY",None)

    def test_recovery_after_external_publication_does_not_duplicate(self):
        os.environ["ARMORED_STUDIO_ALLOW_COPY"]="1"
        os.environ["ARMORED_STUDIO_FORCE_COPY"]="1"
        try:
            with tempfile.TemporaryDirectory() as td:
                root=Path(td); input_dir=root/"input"; input_dir.mkdir()
                source=input_dir/"e2e-300.mp4"; source.write_bytes(b"RECOVERY-PUBLICATION")
                published=set()
                coordinator=Coordinator.build(root, Bindings(Source(source),Vision(),ArmoredStudio(root),None))
                crashing=PersistentPublisher(published,crash_after_publish=True,db=coordinator.db)
                coordinator.pipeline.publisher=crashing
                item_id=coordinator.ingest_once()
                coordinator.run(item_id)
                self.assertEqual(crashing.count,1)
                self.assertEqual(coordinator.db.get(item_id).state,State.RECOVERY)
                self.assertFalse(coordinator.db.publication(item_id)["confirmed"])
                coordinator.close()

                coordinator=Coordinator.build(root, Bindings(Source(source),Vision(),ArmoredStudio(root),None))
                recovered=PersistentPublisher(published,crash_after_publish=False,db=coordinator.db)
                coordinator.pipeline.publisher=recovered
                coordinator.recovery.pipeline.publisher=recovered
                coordinator.recover(item_id)

                row=coordinator.db.get(item_id)
                self.assertEqual(row.state,State.PUBLISHED)
                self.assertEqual(recovered.count,0)
                self.assertEqual(coordinator.db.publication(item_id)["published_message_id"],"e2e-message-e2e-100")
                self.assertTrue(row.original_path.exists())
                self.assertEqual([p.name for p in row.workspace.iterdir()],[row.original_path.name])
                coordinator.run(item_id)
                self.assertEqual(recovered.count,0)
                coordinator.close()
        finally:
            os.environ.pop("ARMORED_STUDIO_ALLOW_COPY",None); os.environ.pop("ARMORED_STUDIO_FORCE_COPY",None)


    def test_recover_pending_finalizes_confirmed_recovery_and_cleans_up(self):
        os.environ["ARMORED_STUDIO_ALLOW_COPY"]="1"
        os.environ["ARMORED_STUDIO_FORCE_COPY"]="1"
        try:
            with tempfile.TemporaryDirectory() as td:
                root=Path(td); input_dir=root/"input"; input_dir.mkdir()
                source=input_dir/"e2e-400.mp4"; source.write_bytes(b"RECOVERY-PENDING-FINALIZE")
                published=set()

                crashing_publisher=PersistentPublisher(
                    published,
                    crash_after_publish=True,
                )
                coordinator=Coordinator.build(
                    root,
                    Bindings(Source(source),Vision(),ArmoredStudio(root),crashing_publisher),
                )
                item_id=coordinator.ingest_once()
                coordinator.run(item_id)
                self.assertEqual(coordinator.db.get(item_id).state,State.RECOVERY)

                # Simulate the durable publication acknowledgement that startup
                # reconciliation receives from Telegram, without republishing.
                coordinator.db.publication_message_sent(
                    item_id,
                    f"e2e-message-{item_id}",
                )
                coordinator.close()

                recovered_publisher=PersistentPublisher(
                    published,
                    crash_after_publish=False,
                )
                coordinator=Coordinator.build(
                    root,
                    Bindings(Source(source),Vision(),ArmoredStudio(root),recovered_publisher),
                )
                recovered=coordinator.recover_pending()

                row=coordinator.db.get(item_id)
                self.assertEqual(recovered,[str(item_id)])
                self.assertEqual(row.state,State.PUBLISHED)
                self.assertTrue(row.cleanup_completed)
                self.assertEqual(recovered_publisher.count,0)
                self.assertTrue(row.original_path.exists())
                self.assertEqual(
                    [p.name for p in row.workspace.iterdir()],
                    [row.original_path.name],
                )
                pub=coordinator.db.publication(item_id)
                self.assertEqual(pub["published_message_id"],f"e2e-message-{item_id}")
                self.assertTrue(pub["confirmed"])
                coordinator.close()
        finally:
            os.environ.pop("ARMORED_STUDIO_ALLOW_COPY",None)
            os.environ.pop("ARMORED_STUDIO_FORCE_COPY",None)

    def test_recovery_with_existing_result_does_not_rerun_studio(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_dir = root / "input"
            input_dir.mkdir()
            source = input_dir / "e2e-600.mp4"
            source.write_bytes(b"RECOVERY-RESULT")

            class CountingVision:
                def __init__(self):
                    self.count = 0
                def identify(self, item):
                    self.count += 1
                    return VisionResult("product-600", "https://example.invalid/affiliate-600")

            class CountingStudio:
                def __init__(self):
                    self.count = 0
                def process(self, item):
                    self.count += 1
                    raise AssertionError("Studio não pode rodar quando o resultado já existe")

            vision = CountingVision()
            studio = CountingStudio()
            publisher = PersistentPublisher()
            coordinator = Coordinator.build(
                root,
                Bindings(Source(source), vision, studio, publisher),
            )
            item_id = coordinator.ingest_once()
            item = coordinator.db.get(item_id)
            coordinator.db.set_vision(
                item_id,
                "product-600",
                "https://example.invalid/affiliate-600",
            )

            result = item.workspace / "600_result.mp4"
            result.write_bytes(b"READY-RESULT")
            coordinator.db.set_result(item_id, result)
            coordinator.db.transition(item_id, State.RECOVERY, "test-publication-unknown")

            coordinator.run(item_id)

            row = coordinator.db.get(item_id)
            self.assertEqual(row.state, State.PUBLISHED)
            self.assertEqual(studio.count, 0)
            self.assertEqual(vision.count, 0)
            self.assertEqual(publisher.count, 1)
            self.assertTrue(row.original_path.exists())
            self.assertEqual([p.name for p in row.workspace.iterdir()], [row.original_path.name])
            coordinator.close()

    def test_recovery_without_result_rebuilds_from_original(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_dir = root / "input"
            input_dir.mkdir()
            source = input_dir / "e2e-601.mp4"
            source.write_bytes(b"RECOVERY-REBUILD")

            class CountingStudio:
                def __init__(self):
                    self.count = 0
                def process(self, item):
                    self.count += 1
                    result = item.workspace / "601_result.mp4"
                    result.write_bytes(b"REBUILT")
                    return type("StudioResult", (), {
                        "working_path": None,
                        "result_path": result,
                    })()

            publisher = PersistentPublisher()
            studio = CountingStudio()
            coordinator = Coordinator.build(
                root,
                Bindings(Source(source), Vision(), studio, publisher),
            )
            item_id = coordinator.ingest_once()
            coordinator.db.set_vision(
                item_id,
                "product-601",
                "https://example.invalid/affiliate-601",
            )
            coordinator.db.transition(item_id, State.RECOVERY, "test-missing-result")

            coordinator.run(item_id)

            row = coordinator.db.get(item_id)
            self.assertEqual(row.state, State.PUBLISHED)
            self.assertEqual(studio.count, 1)
            self.assertEqual(publisher.count, 1)
            self.assertTrue(row.original_path.exists())
            self.assertEqual([p.name for p in row.workspace.iterdir()], [row.original_path.name])
            coordinator.close()

    def test_catch_up_blocks_next_candidate_when_recovery_remains_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_dir = root / "input"
            input_dir.mkdir()
            source_file = input_dir / "e2e-500.mp4"
            source_file.write_bytes(b"RECOVERY-BLOCK")

            initial_source = Source(source_file)
            coordinator = Coordinator.build(
                root,
                Bindings(initial_source, Vision(), ArmoredStudio(root), PersistentPublisher()),
            )
            item_id = coordinator.ingest_once()

            from armored_core.production_contracts import SourceMessage

            class CatchUpSource:
                _historical_limit = None
                historical_materialization_failed = False
                historical_scan_exhausted = False

                def __init__(self, path):
                    self.path = path
                    self.calls = 0

                async def fetch_next_async(self):
                    self.calls += 1
                    if self.calls == 1:
                        return SourceMessage(
                            self.path,
                            str(item_id),
                            "local",
                            original_url="https://example.invalid/product",
                        )
                    if self.calls == 2:
                        return SourceMessage(
                            self.path,
                            "e2e-501",
                            "local",
                            original_url="https://example.invalid/product",
                        )
                    return None

            catch_up_source = CatchUpSource(source_file)
            coordinator.source = catch_up_source

            def enter_recovery(content_id):
                coordinator.db.transition(
                    content_id,
                    State.RECOVERY,
                    "test-publication-unknown",
                )

            coordinator.run = enter_recovery
            recovery_calls = []

            def recover_for_normal_path(content_id):
                recovery_calls.append(str(content_id))
                raise RuntimeError("publication-check-uncertain-recovery-stopped")

            coordinator.recover = recover_for_normal_path

            processed = __import__("asyncio").run(coordinator.run_catch_up_async())

            # CATCH-UP must finish its historical scan before the separate
            # Recovery phase. An unresolved RECOVERY item blocks checkpoint
            # advancement, but does not prevent later candidates from being
            # scanned in the same CATCH-UP pass.
            self.assertEqual(processed, [str(item_id), "e2e-501"])
            self.assertEqual(catch_up_source.calls, 3)
            self.assertEqual(recovery_calls, [])
            self.assertEqual(coordinator.db.get(item_id).state, State.RECOVERY)
            self.assertEqual(coordinator.db.get("e2e-501").state, State.RECOVERY)
            coordinator.close()


    def test_catch_up_stops_when_pipeline_raises_after_entering_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_dir = root / "input"
            input_dir.mkdir()
            source_file = input_dir / "e2e-502.mp4"
            source_file.write_bytes(b"RECOVERY-RAISE")

            initial_source = Source(source_file)
            coordinator = Coordinator.build(
                root,
                Bindings(initial_source, Vision(), ArmoredStudio(root), PersistentPublisher()),
            )
            item_id = coordinator.ingest_once()

            from armored_core.production_contracts import SourceMessage

            class CatchUpSource:
                _historical_limit = None
                historical_materialization_failed = False
                historical_scan_exhausted = False

                def __init__(self, path):
                    self.path = path
                    self.calls = 0

                async def fetch_next_async(self):
                    self.calls += 1
                    if self.calls == 1:
                        return SourceMessage(
                            self.path,
                            str(item_id),
                            "local",
                            original_url="https://example.invalid/product",
                        )
                    if self.calls == 2:
                        return SourceMessage(
                            self.path,
                            "e2e-503",
                            "local",
                            original_url="https://example.invalid/product",
                        )
                    return None

            catch_up_source = CatchUpSource(source_file)
            coordinator.source = catch_up_source

            def pipeline_failure(content_id):
                coordinator.db.transition(
                    content_id,
                    State.RECOVERY,
                    "test-pipeline-raised-after-recovery",
                )
                raise RuntimeError("simulated recoverable pipeline failure")

            coordinator.run = pipeline_failure
            coordinator.recover = lambda content_id: (_ for _ in ()).throw(
                RuntimeError("publication-check-uncertain-recovery-stopped")
            )

            processed = __import__("asyncio").run(coordinator.run_catch_up_async())

            self.assertEqual(processed, [str(item_id), "e2e-503"])
            self.assertEqual(catch_up_source.calls, 3)
            self.assertEqual(coordinator.db.get(item_id).state, State.RECOVERY)
            self.assertEqual(coordinator.db.get("e2e-503").state, State.RECOVERY)
            coordinator.close()


if __name__=="__main__":
    unittest.main()
