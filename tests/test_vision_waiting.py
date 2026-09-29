import tempfile
import unittest
from pathlib import Path

from armored_core.database import Database
from armored_core.models import PublicationCheck, State
from armored_core.pipeline import Pipeline
from armored_core.services import PublicationResult, StudioResult, VisionResult, VisionUnresolvedError
from armored_core.storage import Storage
from ArmoredVision.service import ArmoredVision


class _UnresolvedVision:
    def identify(self, item):
        raise VisionUnresolvedError("produto não resolvido pela V1")


class _Studio:
    def __init__(self):
        self.calls = 0

    def process(self, item):
        self.calls += 1
        raise AssertionError("Studio não pode ser chamado para WAITING_VISION")


class _Publisher:
    def __init__(self):
        self.calls = 0

    def check_publication(self, item):
        self.calls += 1
        return PublicationCheck.ABSENT

    def publish(self, item):
        self.calls += 1
        return PublicationResult(True, "unexpected")


class _NotFoundAPI:
    def get_exact_product(self, shop_id, item_id):
        from ArmoredVision.modules.v1.shopee_api import ShopeeProductNotFoundError
        raise ShopeeProductNotFoundError(f"Produto não encontrado: {shop_id}:{item_id}")


class VisionWaitingTests(unittest.TestCase):
    def _item(self, db, storage, item_id="vision-waiting-1"):
        source = storage.original(item_id, ".mp4", original_url="https://shopee.com.br/a-i.123.456")
        source.write_bytes(b"original")
        db.create_item(
            telegram_message_id=item_id,
            original_path=source,
            original_url="https://shopee.com.br/a-i.123.456",
        )
        return item_id

    def test_v1_unresolved_goes_to_waiting_without_studio_or_hub(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            item_id = self._item(db, storage)
            studio = _Studio()
            publisher = _Publisher()
            try:
                Pipeline(db, storage, _UnresolvedVision(), studio, publisher).run(item_id)
                item = db.get(item_id)
                self.assertEqual(item.state, State.WAITING_VISION)
                self.assertIn("produto não resolvido", db.conn.execute(
                    "SELECT last_error FROM items WHERE content_id=?", (item_id,)
                ).fetchone()["last_error"])
                self.assertEqual(studio.calls, 0)
                self.assertEqual(publisher.calls, 0)
                event = db.conn.execute(
                    "SELECT new_state FROM state_events WHERE content_id=? ORDER BY id DESC LIMIT 1",
                    (item_id,),
                ).fetchone()
                self.assertEqual(event["new_state"], State.WAITING_VISION.value)
                self.assertTrue(db.get(item_id).original_path.is_file())
            finally:
                db.close()

    def test_real_armored_vision_unresolved_flows_through_pipeline_to_waiting(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            item_id = self._item(db, storage, "vision-real-contract-1")
            studio = _Studio()
            publisher = _Publisher()
            vision = ArmoredVision(api=_NotFoundAPI())
            resolved = type("Resolved", (), {"shop_id": "123", "item_id": "456"})()
            from unittest.mock import patch
            try:
                with patch("ArmoredVision.service.resolve_short_url", return_value=resolved):
                    Pipeline(db, storage, vision, studio, publisher).run(item_id)
                self.assertEqual(db.get(item_id).state, State.WAITING_VISION)
                self.assertEqual(studio.calls, 0)
                self.assertEqual(publisher.calls, 0)
                self.assertTrue(db.get(item_id).original_path.is_file())
            finally:
                db.close()

    def test_waiting_vision_survives_restart_and_is_not_reprocessed_automatically(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            item_id = self._item(db, storage)
            Pipeline(db, storage, _UnresolvedVision(), _Studio(), _Publisher()).run(item_id)
            db.close()

            restarted_db = Database(storage.database / "db.sqlite")
            try:
                self.assertEqual(restarted_db.get(item_id).state, State.WAITING_VISION)
                rows = restarted_db.conn.execute(
                    "SELECT content_id FROM items WHERE state=?",
                    (State.WAITING_VISION.value,),
                ).fetchall()
                self.assertEqual([row["content_id"] for row in rows], [item_id])
            finally:
                restarted_db.close()

    def test_coordinator_startup_does_not_auto_retry_waiting_vision(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            item_id = self._item(db, storage, "vision-waiting-startup-1")
            vision = _UnresolvedVision()
            studio = _Studio()
            publisher = _Publisher()
            Pipeline(db, storage, vision, studio, publisher).run(item_id)

            from armored_core.coordinator import Coordinator
            coordinator = Coordinator(
                db, storage, vision, studio, publisher, source=None
            )
            try:
                recovered = coordinator.recover_pending()
                self.assertEqual(recovered, [])
                self.assertEqual(db.get(item_id).state, State.WAITING_VISION)
                self.assertTrue(db.get(item_id).original_path.is_file())
            finally:
                db.close()

    def test_caption_exhaustion_goes_to_recovery_without_waiting_vision(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            item_id = self._item(db, storage, "caption-exhausted-1")
            from unittest.mock import patch

            class _CaptionExhausted:
                def generate(self, product):
                    from ArmoredVision.modules.v1.caption.generator import CaptionGenerationError
                    raise CaptionGenerationError("Nenhuma das 10 candidata(s) passou pela Policy")

            api = type("API", (), {
                "get_exact_product": lambda self, shop_id, item_id: {"productName": "Bancada Suspensa"},
                "affiliate_link_for_product": lambda self, product: "https://affiliate.invalid/456",
            })()
            vision = ArmoredVision(api=api, caption_generator=_CaptionExhausted())
            publisher = _Publisher()
            studio = _Studio()
            resolved = type("Resolved", (), {"shop_id": "123", "item_id": "456"})()
            try:
                with patch("ArmoredVision.service.resolve_short_url", return_value=resolved), \
                     patch.dict("os.environ", {"ARMORED_CAPTION_ENABLED": "1"}, clear=False):
                    from ArmoredVision.modules.v1.caption.generator import CaptionGenerationError
                    with self.assertRaisesRegex(CaptionGenerationError, "Nenhuma das 10 candidata"):
                        Pipeline(db, storage, vision, studio, publisher).run(item_id)
                self.assertEqual(db.get(item_id).state, State.RECOVERY)
                event = db.last_state_event(item_id)
                self.assertEqual(event["new_state"], State.RECOVERY.value)
                self.assertIn("CaptionGenerationError", event["reason"])
                self.assertEqual(studio.calls, 0)
                self.assertEqual(publisher.calls, 0)
                self.assertTrue(db.get(item_id).original_path.is_file())
            finally:
                db.close()

    def test_armored_vision_maps_exact_not_found_to_unresolved(self):
        item = type("ItemStub", (), {
            "original_url": "https://shopee.com.br/a-i.123.456",
        })()
        vision = ArmoredVision(api=_NotFoundAPI())
        from unittest.mock import patch
        resolved = type("Resolved", (), {"shop_id": "123", "item_id": "456"})()
        with patch("ArmoredVision.service.resolve_short_url", return_value=resolved):
            with self.assertRaises(VisionUnresolvedError):
                vision.identify(item)


if __name__ == "__main__":
    unittest.main()
