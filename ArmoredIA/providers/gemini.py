from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

import requests

from .base import AIProviderError


def _context_text(product: dict[str, Any]) -> str:
    fields = (
        ("Nome interno", "productName"),
        ("ID do item", "itemId"),
        ("ID da loja", "shopId"),
        ("Loja", "shopName"),
        ("Categorias", "productCatIds"),
        ("Preço mínimo", "priceMin"),
        ("Preço máximo", "priceMax"),
        ("Vendas", "sales"),
        ("Avaliação", "ratingStar"),
        ("Marca", "brand"),
        ("Marca alternativa", "brandName"),
        ("Modelo", "model"),
        ("Modelo alternativo", "modelName"),
        ("Descrição", "description"),
        ("Atributos", "attributes"),
        ("Características", "technicalCharacteristics"),
    )
    lines = []
    for label, key in fields:
        value = product.get(key)
        if value not in (None, "", [], {}):
            lines.append(f"{label}: {value}")
    return "\n".join(lines)


class GeminiProvider:
    def __init__(self, requester=None, image_getter=None):
        self.requester = requester or requests.post
        self.image_getter = image_getter or requests.get

    @staticmethod
    def _retryable(exc: requests.RequestException) -> bool:
        if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
            return True
        response = getattr(exc, "response", None)
        return getattr(response, "status_code", None) in {429, 500, 502, 503, 504}

    @staticmethod
    def _extract_candidates(data: dict[str, Any], limit: int) -> list[str]:
        try:
            raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
            payload = json.loads(str(raw_text))
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AIProviderError("Gemini não retornou JSON estruturado") from exc

        raw = payload.get("captions") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            raise AIProviderError("Gemini não retornou a lista de candidatas")

        candidates: list[str] = []
        for entry in raw:
            text = str(entry.get("text") if isinstance(entry, dict) else entry or "").strip()
            if text and text not in candidates:
                candidates.append(text)
            if len(candidates) >= limit:
                break
        if not candidates:
            raise AIProviderError("Gemini retornou zero candidatas")
        return candidates

    def generate_candidates(self, task: str, context: dict[str, Any], limit: int) -> list[str]:
        if task != "caption":
            raise AIProviderError(f"Gemini task não suportada: {task}")

        api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
        if not api_key:
            raise AIProviderError("GEMINI_API_KEY ausente; ArmoredIA Caption obrigatória")

        model = os.getenv("ARMORED_IA_MODEL", "gemini-3.1-flash-lite")
        prompt = """
Você recebe contexto de um produto que JÁ FOI identificado pela Vision V1.
A identidade da Vision V1 é a fonte de verdade. Você NÃO identifica, corrige,
substitui ou renomeia o produto.

Gere até 10 opções de reação curta, natural e específica para este item.
Use categoria, uso, ambiente, estilo, aparência e demais sinais disponíveis.
Evite frases coringa quando o contexto permitir algo melhor.

PARA CADA OPÇÃO:
- Português do Brasil.
- Exatamente 2 ou 3 palavras no texto principal.
- Exatamente UM emoji.
- Exatamente 1 ou 2 hashtags.
- Hashtags podem indicar categoria, uso, ambiente ou característica.
- NÃO use embalagem, tampa, frasco ou lacre.
- NÃO use linguagem de venda, urgência, promoção ou desconto.
- NÃO escreva marca, modelo, quantidade, medida ou voltagem.
- NÃO copie uma expressão distintiva do título.
- Não use hashtags para contornar as regras.
- Uma palavra genérica da categoria é permitida.

Retorne SOMENTE JSON:
{"captions":["opção 1","opção 2","..."]}
""".strip()

        parts: list[dict[str, Any]] = [{
            "text": (
                prompt
                + "\n\nCONTEXTO V1 — identidade já resolvida:\n"
                + _context_text(context)
                + f"\nPúblico-alvo: {os.getenv('ARMORED_IA_AUDIENCE', 'público brasileiro de descoberta e lifestyle')}"
            )
        }]

        image_url = str(context.get("imageUrl") or "").strip()
        if image_url:
            try:
                image = self.image_getter(
                    image_url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=int(os.getenv("ARMORED_IA_IMAGE_TIMEOUT", "10")),
                )
                image.raise_for_status()
                mime = image.headers.get("Content-Type", "image/jpeg").split(";", 1)[0]
                parts.append({
                    "inline_data": {
                        "mime_type": mime,
                        "data": base64.b64encode(image.content).decode("ascii"),
                    }
                })
            except requests.RequestException:
                pass

        attempts = max(1, int(os.getenv("ARMORED_IA_MAX_ATTEMPTS", "5")))
        retry_delay = max(0.0, float(os.getenv("ARMORED_IA_RETRY_DELAY", "2")))

        for attempt in range(1, attempts + 1):
            try:
                response = self.requester(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                    json={
                        "contents": [{"parts": parts}],
                        "generationConfig": {
                            "maxOutputTokens": 400,
                            "responseMimeType": "application/json",
                            "responseSchema": {
                                "type": "OBJECT",
                                "properties": {
                                    "captions": {
                                        "type": "ARRAY",
                                        "items": {"type": "STRING"},
                                        "maxItems": min(limit, 10),
                                    }
                                },
                                "required": ["captions"],
                            },
                        },
                    },
                    timeout=int(os.getenv("ARMORED_IA_API_TIMEOUT", "90")),
                )
                response.raise_for_status()
                return self._extract_candidates(response.json(), min(limit, 10))
            except requests.RequestException as exc:
                if self._retryable(exc) and attempt < attempts:
                    time.sleep(retry_delay)
                    continue
                raise AIProviderError(
                    f"Gemini indisponível após {attempt} tentativa(s): {exc}"
                ) from exc
            except AIProviderError:
                if attempt < attempts:
                    time.sleep(retry_delay)
                    continue
                raise

        raise AIProviderError("Gemini indisponível após todas as tentativas")
