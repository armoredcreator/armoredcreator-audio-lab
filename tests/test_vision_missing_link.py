import unittest
from types import SimpleNamespace

from ArmoredVision.service import ArmoredVision
from armored_core.services import VisionUnresolvedError


class VisionMissingLinkTests(unittest.TestCase):
    def test_missing_shopee_url_is_a_waiting_vision_outcome(self):
        item = SimpleNamespace(original_url=None)

        with self.assertRaisesRegex(VisionUnresolvedError, "WAITING_VISION"):
            ArmoredVision(api=object()).identify(item)

    def test_empty_affiliate_url_is_not_approved_for_download(self):
        api = SimpleNamespace(
            get_exact_product=lambda _shop_id, _item_id: {
                "productName": "produto",
                "itemId": "12",
                "shopId": "34",
            },
            affiliate_link_for_product=lambda _product: "",
        )
        item = SimpleNamespace(original_url="https://shopee.com.br/product/34/12")

        with self.assertRaisesRegex(VisionUnresolvedError, "WAITING_VISION"):
            ArmoredVision(api=api).identify(item)

    def test_product_without_canonical_link_waits_instead_of_becoming_technical_failure(self):
        from ArmoredVision.modules.v1.shopee_api import ShopeeAPIError

        api = SimpleNamespace(
            get_exact_product=lambda _shop_id, _item_id: {
                "productName": "produto",
                "itemId": "12",
                "shopId": "34",
            },
            affiliate_link_for_product=lambda _product: (
                (_ for _ in ()).throw(
                    ShopeeAPIError("Produto não possui productLink canônico para gerar afiliação")
                )
            ),
        )
        item = SimpleNamespace(original_url="https://shopee.com.br/product/34/12")

        with self.assertRaisesRegex(VisionUnresolvedError, "WAITING_VISION"):
            ArmoredVision(api=api).identify(item)


if __name__ == "__main__":
    unittest.main()
