from __future__ import annotations

import base64
import json
import os
import time
from typing import Any, Callable

import requests

from .policy import CaptionPolicyError, validate_caption


class CaptionGenerationError(RuntimeError):
    pass


def _gemini_context(product: dict[str, Any]) -> str:
    """Build context from V1 data without asking Gemini to identify the product.

    productOfferV2 exposes commercial/product metadata, not a full catalog
    description. Optional richer fields are accepted when a future/alternate
    V1 source provides them, but the V1 product identity remains authoritative.
    """
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


class CaptionTransportError(CaptionGenerationError):
    """Gemini/API transport or response-availability failure after retries."""


def _candidate_texts(data: dict[str, Any]) -> list[str]:
    try:
        generated_text = (
            data["candidates"][0]["content"]["parts"][0]["text"]
        )
        payload = json.loads(str(generated_text))
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CaptionTransportError(
            "Gemini não retornou JSON estruturado de candidatas"
        ) from exc

    raw = payload.get("captions") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        raise CaptionTransportError("Gemini não retornou a lista de candidatas")
    candidates: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            text = entry.strip()
        elif isinstance(entry, dict):
            text = str(entry.get("text") or "").strip()
        else:
            text = ""
        if text and text not in candidates:
            candidates.append(text)
        if len(candidates) >= 10:
            break
    if not candidates:
        raise CaptionTransportError("Gemini retornou zero candidatas de legenda")
    return candidates


class CaptionGenerator:
    """One Gemini request -> up to ten candidates -> local Policy selection."""

    def __init__(self, requester: Callable[..., Any] | None = None):
        self.requester = requester or requests.post

    @staticmethod
    def _is_retryable_request_error(exc: requests.RequestException) -> bool:
        if isinstance(
            exc,
            (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
            ),
        ):
            return True
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        return status in {429, 500, 502, 503, 504}

    def generate(self, product: dict[str, Any]) -> str:
        if os.getenv("ARMORED_CAPTION_ENABLED", "0") != "1":
            raise CaptionGenerationError("gerador de legenda desativado")

        api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
        if not api_key:
            raise CaptionGenerationError("GEMINI_API_KEY ausente; legenda Gemini obrigatória")

        model = os.getenv("ARMORED_CAPTION_MODEL", "gemini-3.1-flash-lite")
        prompt = """
Você recebe contexto de um produto que JÁ FOI identificado pela Vision V1.
A identidade definida pela Vision V1 é a fonte de verdade. Você NÃO deve
identificar, corrigir, substituir ou renomear o produto.

Sua tarefa é gerar ATÉ 10 opções de reação curta, natural e espontânea que
pareçam escritas especificamente para ESTE produto. Gere opções diferentes
entre si quando o contexto permitir, sem usar fórmulas fixas.

Use qualquer informação útil disponível no contexto V1 — categoria, descrição,
atributos, características, uso percebido, ambiente, estilo, aparência e
demais sinais relevantes. Não force uma característica quando ela não estiver
sustentada pelos dados.

REGRAS PARA CADA OPÇÃO:
- Português do Brasil.
- A legenda deve combinar diretamente com o produto específico identificado no contexto V1.
- O nome do produto é apenas uma referência interna. Não copie o título completo
  nem reproduza uma expressão distintiva do nome.
- Uma palavra genérica do tipo do item pode ser usada naturalmente.
- O texto principal deve ser uma reação específica ao produto, evitando frases
  coringa que poderiam servir para qualquer item.
- As hashtags devem ser específicas e diretamente relacionadas ao produto,
  categoria, uso, ambiente ou característica percebida.
- Exatamente UM emoji.
- Texto principal: EXATAMENTE 2 ou 3 palavras.
- Exatamente 1 ou 2 hashtags.
- NÃO use embalagem, tampa, frasco, lacre ou termos comerciais.
- NÃO escreva marca ou modelo.
- NÃO repita descrição, atributos ou características técnicas.
- NÃO revele quantidade, medidas, voltagem ou embalagem.
- NÃO use linguagem de venda, urgência, promoção ou desconto.
- NÃO use: compre, comprar, garanta, garantir, aproveite, oferta, promoção,
  desconto, imperdível, corra, não perca ou equivalentes.
- Não use hashtags para contornar essas regras.

Retorne SOMENTE JSON válido neste formato:
{"captions":["opção 1","opção 2","..."]}

A lista pode ter até 10 opções. Não inclua explicações, markdown ou campos extras.
""".strip()

        context = _gemini_context(product)
        parts: list[dict[str, Any]] = [{
            "text": (
                prompt
                + "\n\nCONTEXTO V1 — IDENTIDADE JÁ RESOLVIDA; USE O NOME DO PRODUTO COMO ÂNCORA SEM REPETI-LO:\n"
                + context
                + f"\nPúblico-alvo: {os.getenv('ARMORED_CAPTION_AUDIENCE', 'público brasileiro de descoberta e lifestyle')}"
            )
        }]
        image_url = str(product.get("imageUrl") or "").strip()
        if image_url:
            try:
                image = requests.get(
                    image_url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=int(os.getenv("ARMORED_CAPTION_IMAGE_TIMEOUT", "10")),
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

        attempts = max(1, int(os.getenv("ARMORED_CAPTION_MAX_ATTEMPTS", "5")))
        retry_delay = max(0.0, float(os.getenv("ARMORED_CAPTION_RETRY_DELAY", "2")))
        last_transport_error: Exception | None = None

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
                                        "maxItems": 10,
                                    }
                                },
                                "required": ["captions"],
                            },
                        },
                    },
                    timeout=int(os.getenv("ARMORED_CAPTION_API_TIMEOUT", "90")),
                )
                response.raise_for_status()
            except requests.RequestException as exc:
                if not self._is_retryable_request_error(exc):
                    raise CaptionGenerationError(
                        f"Gemini recusou a solicitação: {exc}"
                    ) from exc
                last_transport_error = exc
                if attempt < attempts:
                    time.sleep(retry_delay)
                    continue
                raise CaptionTransportError(
                    f"Gemini indisponível após {attempts} tentativa(s): {exc}"
                ) from exc

            try:
                data = response.json()
                candidates = _candidate_texts(data)
            except CaptionTransportError as exc:
                last_transport_error = exc
                if attempt < attempts:
                    time.sleep(retry_delay)
                    continue
                raise CaptionTransportError(
                    f"Resposta inválida do Gemini após {attempts} tentativa(s): {exc}"
                ) from exc
            except (ValueError, TypeError, KeyError) as exc:
                last_transport_error = exc
                if attempt < attempts:
                    time.sleep(retry_delay)
                    continue
                raise CaptionTransportError(
                    f"Resposta inválida do Gemini após {attempts} tentativa(s): {exc}"
                ) from exc

            policy_errors: list[str] = []
            for index, candidate in enumerate(candidates, start=1):
                try:
                    return validate_caption(
                        candidate,
                        product_name=str(product.get("productName") or ""),
                        product_context=product,
                    )
                except CaptionPolicyError as exc:
                    policy_errors.append(f"{index}: {exc}")

            # Policy rejection is local and final for this request. Never call
            # Gemini again merely because every returned candidate was rejected.
            detail = "; ".join(policy_errors[:10])
            raise CaptionGenerationError(
                f"Nenhuma das {len(candidates)} candidata(s) passou pela Policy: {detail}"
            )

        raise CaptionTransportError(
            f"Gemini indisponível após {attempts} tentativa(s): {last_transport_error}"
        )
