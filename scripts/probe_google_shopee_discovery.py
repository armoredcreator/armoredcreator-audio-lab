from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from html import unescape

QUERIES = [
    'site:shopee.com.br "Bancada Suspensa" "Barbearia" "90cm"',
    'site:shopee.com.br "Bancada" "Cabeleireiro" "90cm" "Gaveta"',
    'site:shopee.com.br "Penteadeira Para Barbearia" "90CM"',
    'site:shopee.com.br "Bancada Suspensa Cabeleireiro" "90cm"',
]

KNOWN = {
    "1609734117:22794532266",
    "329536801:28939276497",
    "335209020:29176620632",
    "382998202:23198215253",
}

PRODUCT_RE = re.compile(
    r"https?://(?:www\.)?shopee\.com\.br/product/(\d+)/(\d+)",
    re.I,
)

def fetch(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")

def main() -> None:
    all_hits = {}
    errors = []
    for q in QUERIES:
        url = "https://www.google.com/search?" + urllib.parse.urlencode(
            {"q": q, "num": 20, "hl": "pt-BR", "gl": "br"}
        )
        try:
            html = fetch(url)
        except Exception as exc:
            errors.append({"query": q, "error": f"{type(exc).__name__}: {exc}"})
            continue

        html = unescape(html)
        hits = []
        for shop_id, item_id in PRODUCT_RE.findall(html):
            key = f"{shop_id}:{item_id}"
            if key not in all_hits:
                all_hits[key] = {"shop_id": int(shop_id), "item_id": int(item_id), "queries": []}
            if q not in all_hits[key]["queries"]:
                all_hits[key]["queries"].append(q)
            hits.append(key)

        print(json.dumps({"query": q, "hits": sorted(set(hits))}, ensure_ascii=False))

    exact = sorted(set(all_hits) & KNOWN)
    print(json.dumps({
        "status": "GOOGLE_SHOPEE_DISCOVERY_PROBE",
        "queries": len(QUERIES),
        "unique_product_ids": len(all_hits),
        "exact_benchmark_hits": exact,
        "exact_benchmark_recall": len(exact) / len(KNOWN),
        "products": sorted(all_hits.values(), key=lambda x: (x["shop_id"], x["item_id"])),
        "errors": errors,
        "note": "Probe only. No V1/V2 integration and no Shopee API bypass."
    }, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
