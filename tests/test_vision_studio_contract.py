import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ArmoredStudio.service import ArmoredStudio
from ArmoredVision.service import ArmoredVision
from armored_core.models import Item, State
from armored_core.storage import Storage


class FakeAPI:
    def affiliate_link_for_product(self, product):
        return str(product["offerLink"])

    def get_exact_product(self, shop_id, item_id):
        assert shop_id == "123"
        assert item_id == "456"
        return {
            "shopId": "123",
            "itemId": "456",
            "productName": "Produto real",
            "offerLink": "https://shopee.com.br/abc/finaldomeulinknovo",
        }


class VisionStudioContractTests(unittest.TestCase):
    def _item(self, root):
        storage = Storage(root)
        original = storage.original("tg-1", original_url="https://shopee.com.br/abc/123/456")
        original.write_bytes(b"ORIGINAL")
        return Item(
            content_id="tg-1",
            telegram_message_id="tg-1",
            state=State.VISION,
            workspace=storage.workspace("tg-1"),
            original_path=original,
            working_path=None,
            result_path=None,
            affiliate_name=None,
            affiliate_url=None,
            original_url="https://shopee.com.br/abc/123/456",
        ), storage

    def test_vision_maps_exact_shopee_product_to_affiliate_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            item, _ = self._item(Path(td))
            vision = ArmoredVision(FakeAPI())
            resolved = type("Resolved", (), {
                "shop_id": "123",
                "item_id": "456",
                "resolved_url": item.original_url,
            })()
            with patch("ArmoredVision.service.resolve_short_url", return_value=resolved):
                result = vision.identify(item)
            self.assertEqual(result.affiliate_name, "Produto real")
            self.assertEqual(
                result.affiliate_url,
                "https://shopee.com.br/abc/finaldomeulinknovo",
            )

    def test_studio_uses_affiliate_url_tail_for_canonical_result_name(self):
        old_allow = os.environ.get("ARMORED_STUDIO_ALLOW_COPY")
        old_force = os.environ.get("ARMORED_STUDIO_FORCE_COPY")
        os.environ["ARMORED_STUDIO_ALLOW_COPY"] = "1"
        os.environ["ARMORED_STUDIO_FORCE_COPY"] = "1"
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                item, storage = self._item(root)
                item = Item(
                    **{**item.__dict__,
                       "state": State.STUDIO,
                       "affiliate_name": "Produto real",
                       "affiliate_url": "https://shopee.com.br/abc/finaldomeulinknovo"}
                )
                result = ArmoredStudio(root).process(item)
                self.assertIsNone(result.working_path)
                self.assertEqual(
                    result.result_path.name,
                    "tg-1_finaldomeulinknovo.mp4",
                )
                self.assertEqual(
                    sorted(p.name for p in storage.workspace(item.content_id).iterdir()),
                    ["tg-1_456.mp4", "tg-1_finaldomeulinknovo.mp4"],
                )
                self.assertFalse((root / "storage" / "sync").exists())
                self.assertFalse((root / "storage" / "queue").exists())
                self.assertFalse((root / "storage" / "pipeline").exists())
                self.assertEqual(item.original_path.read_bytes(), b"ORIGINAL")
        finally:
            for name, old in (
                ("ARMORED_STUDIO_ALLOW_COPY", old_allow),
                ("ARMORED_STUDIO_FORCE_COPY", old_force),
            ):
                if old is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = old

    def test_studio_does_not_silently_copy_when_ffmpeg_missing(self):
        old_allow = os.environ.pop("ARMORED_STUDIO_ALLOW_COPY", None)
        old_force = os.environ.pop("ARMORED_STUDIO_FORCE_COPY", None)
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                item, _ = self._item(root)
                item = Item(
                    **{**item.__dict__,
                       "state": State.STUDIO,
                       "affiliate_name": "Produto",
                       "affiliate_url": "https://shopee.com.br/a/final"}
                )
                with patch("ArmoredStudio.service.shutil.which", return_value=None):
                    with self.assertRaisesRegex(RuntimeError, "FFmpeg não encontrado"):
                        ArmoredStudio(root).process(item)
        finally:
            if old_allow is not None:
                os.environ["ARMORED_STUDIO_ALLOW_COPY"] = old_allow
            if old_force is not None:
                os.environ["ARMORED_STUDIO_FORCE_COPY"] = old_force


if __name__ == "__main__":
    unittest.main()
