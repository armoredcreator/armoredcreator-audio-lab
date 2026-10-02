from __future__ import annotations
import hashlib, json, os, time
from typing import Any, Optional
import requests

class ShopeeAPIError(RuntimeError): pass


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
    def __init__(self, app_id: Optional[str]=None, secret_key: Optional[str]=None):
        self.app_id=app_id or os.getenv("SHOPEE_APP_ID")
        self.secret_key=secret_key or os.getenv("SHOPEE_SECRET_KEY")
        if not self.app_id or not self.secret_key: raise RuntimeError("SHOPEE_APP_ID/SHOPEE_SECRET_KEY não configurados")
    def _post(self, query:str, variables:dict[str,Any]|None=None):
        body={"query":query,"variables":variables or {}}
        payload=json.dumps(body,ensure_ascii=False,separators=(",",":"))
        last=None
        retries=int(os.getenv("SHOPEE_API_MAX_RETRIES","3"))
        for attempt in range(1,retries+1):
            ts=int(time.time())
            sig=hashlib.sha256(f"{self.app_id}{ts}{payload}{self.secret_key}".encode()).hexdigest()
            try:
                r=requests.post(os.getenv("SHOPEE_AFFILIATE_API_URL","https://open-api.affiliate.shopee.com.br/graphql"),
                    data=payload.encode(),headers={"Content-Type":"application/json","Authorization":f"SHA256 Credential={self.app_id},Timestamp={ts},Signature={sig}"},
                    timeout=int(os.getenv("SHOPEE_API_TIMEOUT","30")))
                r.raise_for_status(); data=r.json()
                if data.get("errors"): raise ShopeeAPIError(str(data["errors"]))
                return data
            except (requests.RequestException,ValueError,ShopeeAPIError) as exc:
                last=exc
                if attempt<retries: time.sleep(float(os.getenv("SHOPEE_API_RETRY_BASE_SECONDS","2"))*attempt)
        raise ShopeeAPIError(f"Falha Shopee: {last}")
    def get_exact_product(self,shop_id:str,item_id:str):
        data=self._post(PRODUCT_OFFER_QUERY,{"itemId":str(item_id),"shopId":str(shop_id),"page":1,"limit":1})
        nodes=data.get("data",{}).get("productOfferV2",{}).get("nodes") or []
        if not nodes: raise ShopeeProductNotFoundError(f"Produto não encontrado: {shop_id}:{item_id}")
        p=nodes[0]
        if str(p.get("shopId"))!=str(shop_id) or str(p.get("itemId"))!=str(item_id): raise ShopeeAPIError("Produto Shopee divergente")
        return p
    def affiliate_link_for_product(self, product: dict[str, Any]):
        offer = str(product.get("offerLink") or "").strip()
        if offer:
            return offer
        product_link = str(product.get("productLink") or "").strip()
        if not product_link:
            raise ShopeeAPIError("Produto não possui productLink canônico para gerar afiliação")
        return str(self.generate_short_link(product_link)["short_link"]).strip()

    def generate_short_link(self,origin_url:str):
        q=f"""mutation {{ generateShortLink(input: {{ originUrl: {json.dumps(origin_url)} }}) {{ shortLink }} }}"""
        link=(self._post(q).get("data",{}).get("generateShortLink") or {}).get("shortLink")
        if not link: raise ShopeeAPIError("Shopee não retornou shortLink")
        return {"short_link":str(link)}
