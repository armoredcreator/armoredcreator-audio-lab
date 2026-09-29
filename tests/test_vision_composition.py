from __future__ import annotations

import os
import unittest

from ArmoredVision.service import ArmoredVision
from ArmoredVision.modules.v1.caption.generator import CaptionTransportError


PRODUCT = {
    "itemId": 123,
    "shopId": 456,
    "productName": "Produto Exemplo 1L",
    "shopName": "Loja Exemplo",
    "productLink": "https://shopee.com.br/product/456/123",
    "offerLink": "https://s.shopee.com.br/original",
    "imageUrl": "",
}


class V1FakeAPI:
    def __init__(self):
        self.exact_calls = []

    def get_exact_product(self, shop_id, item_id):
        self.exact_calls.append((shop_id, item_id))
        return PRODUCT.copy()

    def affiliate_link_for_product(self, product):
        return str(product["offerLink"])

    def generate_short_link(self, origin):
        return "https://s.shopee.com.br/generated"


class FakeCaption:
    def generate(self, product):
        assert product["productName"] == "Produto Exemplo 1L"
        return "Olha esse charme ✨\n#casa"


class FailingCaption:
    def generate(self, product):
        raise RuntimeError("gemini-temporarily-unavailable")


def test_v1_contract_remains_usable_when_caption_is_disabled(monkeypatch):
    monkeypatch.delenv("ARMORED_CAPTION_ENABLED", raising=False)

    api = V1FakeAPI()
    vision = ArmoredVision(api=api)

    result = vision.identify(
        type(
            "ItemStub",
            (),
            {"original_url": "https://shopee.com.br/product/456/123"},
        )()
    )

    assert result.affiliate_name == "Produto Exemplo 1L"
    assert result.affiliate_url == "https://s.shopee.com.br/original"
    assert result.affiliate_urls == ("https://s.shopee.com.br/original",)
    assert result.publication_caption is None
    assert api.exact_calls == [("456", "123")]


def test_caption_is_composed_after_v1_without_changing_v1(monkeypatch):
    monkeypatch.setenv("ARMORED_CAPTION_ENABLED", "1")

    api = V1FakeAPI()
    vision = ArmoredVision(
        api=api,
        caption_generator=FakeCaption(),
    )

    result = vision.identify(
        type(
            "ItemStub",
            (),
            {"original_url": "https://shopee.com.br/product/456/123"},
        )()
    )

    assert result.affiliate_name == "Produto Exemplo 1L"
    assert result.affiliate_url == "https://s.shopee.com.br/original"
    assert result.affiliate_urls == ("https://s.shopee.com.br/original",)
    assert result.publication_caption == "Olha esse charme ✨\n#casa"
    assert api.exact_calls == [("456", "123")]


class VisionCompositionTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("ARMORED_CAPTION_ENABLED", None)


    def test_caption_transport_exhaustion_maps_to_technical_recovery(self):
        os.environ["ARMORED_CAPTION_ENABLED"] = "1"

        class TransportFailingCaption:
            def generate(self, product):
                raise CaptionTransportError("Gemini indisponível após retries")

        vision = ArmoredVision(
            api=V1FakeAPI(),
            caption_generator=TransportFailingCaption(),
        )

        with self.assertRaises(RuntimeError) as ctx:
            vision.identify(
                type(
                    "ItemStub",
                    (),
                    {"original_url": "https://shopee.com.br/product/456/123"},
                )()
            )

        self.assertIn("Caption Gemini indisponível", str(ctx.exception))

    def test_caption_runtime_failure_maps_to_waiting_vision(self):
        os.environ["ARMORED_CAPTION_ENABLED"] = "1"

        vision = ArmoredVision(
            api=V1FakeAPI(),
            caption_generator=FailingCaption(),
        )

        from armored_core.services import VisionUnresolvedError

        with self.assertRaises(VisionUnresolvedError) as ctx:
            vision.identify(
                type(
                    "ItemStub",
                    (),
                    {"original_url": "https://shopee.com.br/product/456/123"},
                )()
            )

        self.assertIn("Caption Generator indisponível", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
