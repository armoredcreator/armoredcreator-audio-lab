from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ArmoredVision.service import ArmoredVision
from ArmoredVision.modules.v1.shopee_resolver import ShopeeResolvedLink
from armored_core.database import Database
from armored_core.storage import Storage


class _FakeAPI:
    def __init__(self):
        self.product = {
            "productName": "Produto Exato",
            "shopId": "123",
            "itemId": "456",
            "offerLink": "",
            "productLink": "https://shopee.com.br/product/123/456",
        }
        self.called_with = None

    def get_exact_product(self, shop_id, item_id):
        self.called_with = (str(shop_id), str(item_id))
        return dict(self.product)

    def affiliate_link_for_product(self, product):
        self.called_with = self.called_with
        return "https://s.shopee.com.br/CANONICAL"

    def generate_short_link(self, origin_url):
        raise AssertionError("Vision não deve gerar link usando a URL recebida")


class VisionCanonicalAffiliateTests(unittest.TestCase):
    def test_identify_uses_product_affiliate_link_not_source_affiliate_url(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "db.sqlite")
            item_id = db.create_item(
                "tg-1",
                storage.original("tg-1"),
                original_url="https://s.shopee.com.br/4fwYHydJri",
            )
            item = db.get(item_id)
            api = _FakeAPI()
            with patch(
                "ArmoredVision.service.resolve_short_url",
                return_value=ShopeeResolvedLink(
                    "https://s.shopee.com.br/4fwYHydJri",
                    "https://shopee.com.br/product/123/456",
                    "123",
                    "456",
                ),
            ):
                result = ArmoredVision(api=api).identify(item)

            self.assertEqual(result.affiliate_name, "Produto Exato")
            self.assertEqual(result.affiliate_url, "https://s.shopee.com.br/CANONICAL")
            self.assertEqual(api.called_with, ("123", "456"))
            db.close()


if __name__ == "__main__":
    unittest.main()
