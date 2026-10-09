from __future__ import annotations

from armored_core.models import Item
from armored_core.services import VisionResult, VisionUnresolvedError

from .modules.v1.shopee_api import (
    ShopeeAffiliateAPI,
    ShopeeAPIError,
    ShopeeProductNotFoundError,
)
from .modules.v1.shopee_resolver import resolve_short_url


class ArmoredVision:
    """Affiliate identity stage.

    It owns Shopee resolution/API concerns only. Pipeline state remains in
    armored_core and no JSON state from the legacy Vision is used.
    """

    def __init__(self, api=None):
        self.api = api

    @staticmethod
    def _ia_context(product: dict) -> dict:
        keys = (
            "productName", "itemId", "shopId", "shopName", "productCatIds",
            "priceMin", "priceMax", "sales", "ratingStar", "brand",
            "brandName", "model", "modelName", "description", "attributes",
            "technicalCharacteristics", "imageUrl",
        )
        return {
            key: product[key]
            for key in keys
            if key in product and product[key] not in (None, "", [], {})
        }

    def identify(self, item: Item) -> VisionResult:
        original = (item.original_url or "").strip()
        if not original:
            raise VisionUnresolvedError(
                "Vision não recebeu link Shopee; vídeo preservado em WAITING_VISION"
            )

        try:
            resolved = resolve_short_url(original)
        except ValueError as exc:
            # A syntactically valid Shopee/creator URL that does not identify
            # one exact product is a Vision classification outcome, not a
            # technical pipeline failure. Persist WAITING_VISION so historical
            # catch-up can advance without retrying the same non-product URL.
            raise VisionUnresolvedError(
                f"Vision V1 não conseguiu identificar um produto Shopee exato: {original}"
            ) from exc

        api = self.api or ShopeeAffiliateAPI()
        try:
            product = api.get_exact_product(resolved.shop_id, resolved.item_id)
        except ShopeeProductNotFoundError as exc:
            raise VisionUnresolvedError(
                f"Vision V1 não resolveu o produto Shopee "
                f"{resolved.shop_id}:{resolved.item_id}; "
                "item preservado para futura recuperação"
            ) from exc

        # The incoming URL is identification input only. The final affiliate
        # link must come from the exact V1-resolved product, never from a
        # third-party affiliate short URL supplied by the source message.
        try:
            affiliate_url = str(api.affiliate_link_for_product(product) or "").strip()
        except ShopeeAPIError as exc:
            # A product record without a canonical product URL is not an
            # accepted affiliate offer. Preserve it as WAITING_VISION rather
            # than treating it as approved or repeatedly retrying a known
            # unavailable offer. Other Shopee API errors remain technical
            # failures so they can be retried safely.
            if "não possui productLink canônico" in str(exc):
                raise VisionUnresolvedError(
                    "Vision encontrou o produto, mas não há link canônico "
                    "para gerar oferta de afiliado; item preservado em WAITING_VISION"
                ) from exc
            raise

        if not affiliate_url.lower().startswith(("https://", "http://")):
            raise VisionUnresolvedError(
                "Vision não recebeu URL de afiliado válida; "
                "item preservado em WAITING_VISION"
            )

        identifier = str(
            product.get("productName")
            or product.get("itemId")
            or f"{resolved.shop_id}_{resolved.item_id}"
        )

        return VisionResult(
            identifier,
            affiliate_url,
            affiliate_urls=(affiliate_url,),
            publication_caption=None,
            ia_context=self._ia_context(product),
        )


def build(**_kwargs):
    return ArmoredVision()
