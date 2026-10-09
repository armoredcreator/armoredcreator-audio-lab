from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Optional

import requests


class ShopeeAPIError(RuntimeError):
    """Shopee API failure; GraphQL/business errors are not retried by default."""


class ShopeeProductNotFoundError(ShopeeAPIError):
    """Exact V1 lookup found no affiliate offer for the supplied IDs."""


PRODUCT_OFFER_QUERY = """
query ProductOffer($itemId: Int64, $shopId: Int64, $page: Int, $limit: Int) {
  productOfferV2(itemId: $itemId, shopId: $shopId, page: $page, limit: $limit) {
    nodes { itemId commissionRate commission price sales imageUrl productName shopName productLink offerLink ratingStar shopId }
  }
}
"""


class ShopeeAffiliateAPI:
    def __init__(self, app_id: Optional[str] = None, secret_key: Optional[str] = None):
        self.app_id = app_id or os.getenv("SHOPEE_APP_ID")
        self.secret_key = secret_key or os.getenv("SHOPEE_SECRET_KEY")
        if not self.app_id or not self.secret_key:
            raise RuntimeError("SHOPEE_APP_ID/SHOPEE_SECRET_KEY não configurados")

    def _post(self, query: str, variables: dict[str, Any] | None = None):
        body = {"query": query, "variables": variables or {}}
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        last = None
        retries = max(1, int(os.getenv("SHOPEE_API_MAX_RETRIES", "3")))
        timeout = max(1, int(os.getenv("SHOPEE_API_TIMEOUT", "30")))
        retry_base = max(0.0, float(os.getenv("SHOPEE_API_RETRY_BASE_SECONDS", "2")))

        for attempt in range(1, retries + 1):
            ts = int(time.time())
            sig = hashlib.sha256(
                f"{self.app_id}{ts}{payload}{self.secret_key}".encode()
            ).hexdigest()
            try:
                response = requests.post(
                    os.getenv(
                        "SHOPEE_AFFILIATE_API_URL",
                        "https://open-api.affiliate.shopee.com.br/graphql",
                    ),
                    data=payload.encode(),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": (
                            f"SHA256 Credential={self.app_id},Timestamp={ts},Signature={sig}"
                        ),
                    },
                    timeout=timeout,
                )
                response.raise_for_status()
                data = response.json()
                # GraphQL errors are usually validation, permission, or request
                # errors. Retrying them blindly wastes time and can hide a bad
                # configuration. Only transport/HTTP transient failures retry.
                if data.get("errors"):
                    raise ShopeeAPIError(f"Erro GraphQL Shopee: {data['errors']}")
                return data
            except ShopeeAPIError as exc:
                last = exc
                retryable = False
            except requests.HTTPError as exc:
                last = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                retryable = status == 429 or (status is not None and status >= 500)
            except (requests.RequestException, ValueError) as exc:
                last = exc
                retryable = True

            if not retryable or attempt >= retries:
                break
            if retry_base:
                time.sleep(retry_base * attempt)

        raise ShopeeAPIError(f"Falha Shopee: {last}")

    def get_exact_product(self, shop_id: str, item_id: str):
        data = self._post(
            PRODUCT_OFFER_QUERY,
            {"itemId": str(item_id), "shopId": str(shop_id), "page": 1, "limit": 1},
        )
        nodes = data.get("data", {}).get("productOfferV2", {}).get("nodes") or []
        if not nodes:
            raise ShopeeProductNotFoundError(
                f"Produto não encontrado: {shop_id}:{item_id}"
            )
        product = nodes[0]
        if str(product.get("shopId")) != str(shop_id) or str(product.get("itemId")) != str(item_id):
            raise ShopeeAPIError("Produto Shopee divergente")
        return product

    def affiliate_link_for_product(self, product: dict[str, Any]):
        offer = str(product.get("offerLink") or "").strip()
        if offer:
            return offer
        product_link = str(product.get("productLink") or "").strip()
        if not product_link:
            raise ShopeeAPIError(
                "Produto não possui productLink canônico para gerar afiliação"
            )
        return str(self.generate_short_link(product_link)["short_link"]).strip()

    def generate_short_link(self, origin_url: str):
        query = f"""mutation {{ generateShortLink(input: {{ originUrl: {json.dumps(origin_url)} }}) {{ shortLink }} }}"""
        link = (
            self._post(query).get("data", {}).get("generateShortLink") or {}
        ).get("shortLink")
        if not link:
            raise ShopeeAPIError("Shopee não retornou shortLink")
        return {"short_link": str(link)}
