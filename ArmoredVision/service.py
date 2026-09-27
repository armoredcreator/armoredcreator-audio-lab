from __future__ import annotations

import os

from armored_core.models import Item
from armored_core.services import VisionResult, VisionUnresolvedError

from .modules.v1.shopee_api import ShopeeAffiliateAPI, ShopeeProductNotFoundError
from .modules.v1.shopee_resolver import resolve_short_url
from .modules.v1.caption.generator import CaptionGenerator, CaptionGenerationError


class ArmoredVision:
    """Affiliate identity stage.

    It owns Shopee resolution/API concerns only. Pipeline state remains in
    armored_core and no JSON state from the legacy Vision is used.
    """

    def __init__(self, api=None, caption_generator=None):
        self.api = api
        self.caption_generator = caption_generator

    def identify(self, item: Item) -> VisionResult:
        original = (item.original_url or "").strip()
        if not original:
            raise RuntimeError("Vision: original Shopee URL ausente")

        resolved = resolve_short_url(original)
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
        affiliate_url = str(api.affiliate_link_for_product(product)).strip()

        identifier = str(
            product.get("productName")
            or product.get("itemId")
            or f"{resolved.shop_id}_{resolved.item_id}"
        )

        publication_caption = None
        if os.getenv("ARMORED_CAPTION_ENABLED", "0") == "1":
            try:
                generator = self.caption_generator or CaptionGenerator()
                publication_caption = generator.generate(product)
            except CaptionGenerationError as exc:
                raise VisionUnresolvedError(str(exc)) from exc
            except Exception as exc:
                raise VisionUnresolvedError(
                    f"Caption Generator indisponível: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

        return VisionResult(
            identifier,
            affiliate_url,
            affiliate_urls=(affiliate_url,),
            publication_caption=publication_caption,
        )


def build(**_kwargs):
    return ArmoredVision()
