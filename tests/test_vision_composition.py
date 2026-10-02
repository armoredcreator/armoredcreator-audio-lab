from __future__ import annotations

from ArmoredVision.service import ArmoredVision


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


def test_v1_resolves_product_without_ai_execution(monkeypatch):
    monkeypatch.setenv("ARMORED_IA_ENABLED", "1")
    monkeypatch.setenv("ARMORED_IA_CAPTION_ENABLED", "1")

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
    assert result.ia_context["productName"] == "Produto Exemplo 1L"
    assert result.ia_context["shopName"] == "Loja Exemplo"
    assert api.exact_calls == [("456", "123")]
