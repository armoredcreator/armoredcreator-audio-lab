from __future__ import annotations

import os
import unittest

import requests

from ArmoredVision.modules.v1.caption.generator import CaptionGenerationError, CaptionGenerator
from ArmoredVision.modules.v1.caption.policy import CaptionPolicyError, validate_caption


class CaptionPolicyTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("ARMORED_CAPTION_ENABLED", None)
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ.pop("ARMORED_CAPTION_ALLOW_DETERMINISTIC_FALLBACK", None)
        os.environ.pop("ARMORED_CAPTION_API_TIMEOUT", None)
        os.environ.pop("ARMORED_CAPTION_MODEL", None)
        os.environ.pop("ARMORED_CAPTION_MAX_ATTEMPTS", None)
        os.environ.pop("ARMORED_CAPTION_RETRY_DELAY", None)

    def test_accepts_required_shape(self):
        result = validate_caption(
            "Cores organizadas ✨\n#unhas #organizacao",
            product_name="Expositor de esmaltes com gavetas",
        )
        self.assertEqual(result, "Cores organizadas ✨\n#unhas #organizacao")

    def test_rejects_forbidden_and_invalid_rules(self):
        invalid = (
            "Que embalagem linda ✨\n#casa",
            "Olha só esse produto incrível ✨\n#casa",
            "Olha esse charme 😍✨\n#casa",
            "Olha esse charme ✨\n#casa #rotina #achadinhos",
            "Olha esse charme demais ✨\n#casa",
        )
        for caption in invalid:
            with self.subTest(caption=caption):
                with self.assertRaises(CaptionPolicyError):
                    validate_caption(caption, product_name="Organizador de cozinha")

        product_leaks = (
            ("Batom lindo ✨\n#beleza", "Batom Matte Vermelho"),
            ("Olha isso ✨\n#batommatte", "Batom Matte Vermelho"),
        )
        for caption, product_name in product_leaks:
            with self.subTest(caption=caption):
                with self.assertRaises(CaptionPolicyError):
                    validate_caption(caption, product_name=product_name)

    def test_generator_falls_back_when_gemini_times_out(self):
        os.environ["ARMORED_CAPTION_ENABLED"] = "1"
        os.environ["GEMINI_API_KEY"] = "configured-but-unavailable"
        os.environ["ARMORED_CAPTION_ALLOW_DETERMINISTIC_FALLBACK"] = "1"

        def requester(*args, **kwargs):
            raise __import__("requests").exceptions.ReadTimeout("timeout")

        caption = CaptionGenerator(requester=requester).generate({
            "productName": "Batom Matte Vermelho",
            "category_name": "beleza",
        })

        self.assertEqual(
            validate_caption(caption, product_name="Batom Matte Vermelho"),
            caption,
        )

    def test_rejects_brand_model_and_description_leaks(self):
        context = {
            "brand": "Tramontina",
            "model": "Pro 900",
            "description": "estrutura de aço carbono resistente",
        }
        invalid = (
            "Tramontina linda ✨\n#casa",
            "Pro 900 lindo ✨\n#casa",
            "Carbono resistente ✨\n#casa",
        )
        for caption in invalid:
            with self.subTest(caption=caption):
                with self.assertRaises(CaptionPolicyError):
                    validate_caption(caption, product_name="Bancada Suspensa", product_context=context)

    def test_gemini_receives_richer_v1_context_without_affiliate_fields(self):
        os.environ["ARMORED_CAPTION_ENABLED"] = "1"
        os.environ["GEMINI_API_KEY"] = "configured"
        os.environ["ARMORED_CAPTION_ALLOW_DETERMINISTIC_FALLBACK"] = "0"
        captured = {}

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"candidates": [{"content": {"parts": [{"text": "Cantinho profissional ✨\n#barbearia #organizacao"}]}}]}

        def requester(*args, **kwargs):
            captured["json"] = kwargs["json"]
            captured["timeout"] = kwargs["timeout"]
            return Response()

        product = {
            "productName": "Bancada Suspensa",
            "itemId": "123",
            "shopId": "456",
            "shopName": "Loja Exemplo",
            "productCatIds": [100, 200],
            "priceMin": "199.90",
            "priceMax": "299.90",
            "sales": 42,
            "ratingStar": "4.9",
            "brand": "Marca Exemplo",
            "model": "Modelo X",
            "description": "estrutura de aço carbono resistente",
            "offerLink": "https://affiliate.invalid/secret",
            "productLink": "https://shopee.invalid/product",
        }
        caption = CaptionGenerator(requester=requester).generate(product)

        self.assertEqual(caption, "Cantinho profissional ✨\n#barbearia #organizacao")
        prompt_text = captured["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("Categorias: [100, 200]", prompt_text)
        self.assertIn("Marca: Marca Exemplo", prompt_text)
        self.assertIn("Modelo: Modelo X", prompt_text)
        self.assertIn("Descrição: estrutura de aço carbono resistente", prompt_text)
        self.assertNotIn("affiliate.invalid", prompt_text)
        self.assertNotIn("shopee.invalid", prompt_text)
        self.assertIn("combinar diretamente com o produto específico", prompt_text)
        self.assertIn("NUNCA copie o nome do produto", prompt_text)
        self.assertIn("hashtags também DEVEM ser específicas", prompt_text)
        self.assertEqual(captured["timeout"], 90)
    def test_generator_falls_back_when_gemini_returns_invalid_caption(self):
        os.environ["ARMORED_CAPTION_ENABLED"] = "1"
        os.environ["GEMINI_API_KEY"] = "configured-but-invalid-response"
        os.environ["ARMORED_CAPTION_ALLOW_DETERMINISTIC_FALLBACK"] = "1"

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"candidates": [{"content": {"parts": [{"text": "Compre agora 🔥\n#oferta #promo #extra"}]}}]}

        caption = CaptionGenerator(requester=lambda *args, **kwargs: Response()).generate({
            "productName": "Batom Matte Vermelho",
            "category_name": "beleza",
        })

        self.assertEqual(
            validate_caption(caption, product_name="Batom Matte Vermelho"),
            caption,
        )

    def test_generator_has_safe_deterministic_fallback(self):
        os.environ["ARMORED_CAPTION_ENABLED"] = "1"
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ["ARMORED_CAPTION_ALLOW_DETERMINISTIC_FALLBACK"] = "1"

        caption = CaptionGenerator().generate({
            "productName": "Kit de cuidados de beleza",
            "category_name": "beleza",
        })

        self.assertEqual(
            validate_caption(caption, product_name="Kit de cuidados de beleza"),
            caption,
        )


    def test_generator_retries_until_gemini_returns_valid_caption(self):
        os.environ["ARMORED_CAPTION_ENABLED"] = "1"
        os.environ["GEMINI_API_KEY"] = "configured"
        os.environ["ARMORED_CAPTION_ALLOW_DETERMINISTIC_FALLBACK"] = "0"
        os.environ["ARMORED_CAPTION_MAX_ATTEMPTS"] = "3"
        os.environ["ARMORED_CAPTION_RETRY_DELAY"] = "0"

        calls = {"count": 0}

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "candidates": [{
                        "content": {
                            "parts": [{
                                "text": "Cantinho profissional " + chr(0x2728) + "\n#barbearia #organizacao"
                            }]
                        }
                    }]
                }

        def requester(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] < 3:
                raise requests.exceptions.ReadTimeout("temporary timeout")
            return Response()

        caption = CaptionGenerator(requester=requester).generate({
            "productName": "Bancada Suspensa",
            "category_name": "barbearia",
        })

        self.assertEqual(calls["count"], 3)
        self.assertEqual(
            caption,
            "Cantinho profissional " + chr(0x2728) + "\n#barbearia #organizacao",
        )

    def test_generator_does_not_fallback_when_gemini_exhausts_retries(self):
        os.environ["ARMORED_CAPTION_ENABLED"] = "1"
        os.environ["GEMINI_API_KEY"] = "configured"
        os.environ["ARMORED_CAPTION_ALLOW_DETERMINISTIC_FALLBACK"] = "0"
        os.environ["ARMORED_CAPTION_MAX_ATTEMPTS"] = "2"
        os.environ["ARMORED_CAPTION_RETRY_DELAY"] = "0"

        calls = {"count": 0}

        def requester(*args, **kwargs):
            calls["count"] += 1
            raise requests.exceptions.ReadTimeout("unavailable")

        with self.assertRaises(CaptionGenerationError):
            CaptionGenerator(requester=requester).generate({
                "productName": "Bancada Suspensa",
                "category_name": "barbearia",
            })

        self.assertEqual(calls["count"], 2)


if __name__ == "__main__":
    unittest.main()
