from __future__ import annotations

import logging
import time
from collections import OrderedDict

from armored_core.models import Item
from armored_core.services import VisionResult, VisionUnresolvedError

from .modules.v1.shopee_api import (
    ShopeeAffiliateAPI,
    ShopeeAPIError,
    ShopeeProductNotFoundError,
)
from .modules.v1.shopee_resolver import resolve_short_url

log = logging.getLogger(__name__)
_RESOLUTION_CACHE_TTL_SECONDS = 300
_PRODUCT_CACHE_TTL_SECONDS = 15
_CACHE_MAX_ENTRIES = 512


class ArmoredVision:
    """Affiliate identity stage with bounded, short-lived in-process memoization.

    Product approval is still based on an exact Shopee productOfferV2 lookup.
    No result survives this process, and successful product results live for at
    most 15 seconds so repeated messages for the same product do not hammer the
    API while stale availability is not treated as durable evidence.
    """

    def __init__(self, api=None):
        self.api = api
        self._resolution_cache: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self._product_cache: OrderedDict[tuple[str, str], tuple[float, dict]] = OrderedDict()

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

    @staticmethod
    def _cache_get(cache, key, ttl):
        entry = cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            cache.pop(key, None)
            return None
        cache.move_to_end(key)
        # Cache values are immutable for resolution and copied for product data.
        return value.copy() if isinstance(value, dict) else value

    @staticmethod
    def _cache_put(cache, key, value, ttl):
        cache[key] = (time.monotonic() + ttl, value.copy() if isinstance(value, dict) else value)
        cache.move_to_end(key)
        while len(cache) > _CACHE_MAX_ENTRIES:
            cache.popitem(last=False)

    def identify(self, item: Item) -> VisionResult:
        started = time.perf_counter()
        original = (item.original_url or "").strip()
        if not original:
            raise VisionUnresolvedError(
                "Vision não recebeu link Shopee; vídeo preservado em WAITING_VISION"
            )

        phase_started = time.perf_counter()
        resolved = self._cache_get(
            self._resolution_cache, original, _RESOLUTION_CACHE_TTL_SECONDS
        )
        resolution_cache_hit = resolved is not None
        if not resolution_cache_hit:
            try:
                resolved = resolve_short_url(original)
            except ValueError as exc:
                raise VisionUnresolvedError(
                    f"Vision V1 não conseguiu identificar um produto Shopee exato: {original}"
                ) from exc
            self._cache_put(
                self._resolution_cache, original, resolved, _RESOLUTION_CACHE_TTL_SECONDS
            )
        resolution_seconds = time.perf_counter() - phase_started

        api = self.api
        if api is None:
            api = ShopeeAffiliateAPI()
            self.api = api

        phase_started = time.perf_counter()
        product_key = (str(resolved.shop_id), str(resolved.item_id))
        product = self._cache_get(
            self._product_cache, product_key, _PRODUCT_CACHE_TTL_SECONDS
        )
        product_cache_hit = product is not None
        if not product_cache_hit:
            try:
                product = api.get_exact_product(*product_key)
            except ShopeeProductNotFoundError as exc:
                raise VisionUnresolvedError(
                    f"Vision V1 não resolveu o produto Shopee "
                    f"{resolved.shop_id}:{resolved.item_id}; "
                    "item preservado para futura recuperação"
                ) from exc
            self._cache_put(
                self._product_cache, product_key, product, _PRODUCT_CACHE_TTL_SECONDS
            )
        product_seconds = time.perf_counter() - phase_started

        phase_started = time.perf_counter()
        try:
            affiliate_url = str(api.affiliate_link_for_product(product) or "").strip()
        except ShopeeAPIError as exc:
            if "não possui productLink canônico" in str(exc):
                raise VisionUnresolvedError(
                    "Vision encontrou o produto, mas não há link canônico "
                    "para gerar oferta de afiliado; item preservado em WAITING_VISION"
                ) from exc
            raise
        link_seconds = time.perf_counter() - phase_started

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
        log.info(
            "[VISION-TIMING] item=%s resolve=%.3fs resolve_cache=%s "
            "product_offer=%.3fs product_cache=%s affiliate_link=%.3fs total=%.3fs",
            getattr(item, "content_id", "<sem-content-id>"),
            resolution_seconds,
            "hit" if resolution_cache_hit else "miss",
            product_seconds,
            "hit" if product_cache_hit else "miss",
            link_seconds,
            time.perf_counter() - started,
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
