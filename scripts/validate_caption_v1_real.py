from __future__ import annotations

import argparse
import os

from ArmoredVision.modules.v1.caption.generator import CaptionGenerator, CaptionGenerationError
from ArmoredVision.modules.v1.shopee_api import ShopeeAffiliateAPI
from ArmoredVision.modules.v1.shopee_resolver import resolve_short_url


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ArmoredCreator - smoke real da Caption V1 (sem Pipeline/Studio/Hub/Telegram)"
    )
    parser.add_argument("--url", required=True, help="URL Shopee do produto")
    parser.add_argument("--require-gemini", action="store_true", help="falha se GEMINI_API_KEY não estiver configurada")
    args = parser.parse_args()

    os.environ["ARMORED_CAPTION_ENABLED"] = "1"

    if args.require_gemini and not os.getenv("GEMINI_API_KEY"):
        raise SystemExit("ERRO: GEMINI_API_KEY não configurada no ambiente atual.")

    resolved = resolve_short_url(args.url)
    api = ShopeeAffiliateAPI()
    product = api.get_exact_product(resolved.shop_id, resolved.item_id)

    print("=" * 72)
    print("ARMORED CREATOR - SMOKE REAL CAPTION V1")
    print("=" * 72)
    print(f"Shop ID:      {resolved.shop_id}")
    print(f"Item ID:      {resolved.item_id}")
    print(f"Produto V1:   {product.get('productName') or '-'}")
    print(f"Loja:         {product.get('shopName') or '-'}")
    print(f"Modelo:       {os.getenv('ARMORED_CAPTION_MODEL', 'gemini-3.1-flash-lite')}")
    print("V2:           DESATIVADA / não utilizada")
    print()

    try:
        caption = CaptionGenerator().generate(product)
    except CaptionGenerationError as exc:
        print("RESULTADO: FALHOU")
        print(f"MOTIVO: {exc}")
        return 1

    print("RESULTADO: PASSOU")
    print("LEGENDA REAL:")
    print(caption)
    print()
    print("Nenhuma publicação, Studio ou alteração de SQLite foi executada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
