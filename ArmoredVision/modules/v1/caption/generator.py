from __future__ import annotations

import base64
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


class CaptionGenerator:
    """Gemini-only caption generator; pipeline advances only with a valid Gemini caption."""

    def __init__(self, requester: Callable[..., Any] | None = None):
        self.requester = requester or requests.post

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

Sua tarefa é interpretar livremente o contexto e criar uma reação curta, natural e espontânea que pareça escrita especificamente para ESTE produto. Não siga fórmulas, padrões de frases ou vocabulário fixo. Produtos diferentes devem naturalmente produzir reações diferentes quando o contexto justificar.\n\nUse qualquer informação útil disponível no contexto V1 — categoria, descrição, atributos, características, uso percebido, ambiente, estilo, aparência e demais sinais relevantes. Não force uma característica quando ela não estiver sustentada pelos dados.\n\nREGRAS:
- Português do Brasil.
- A legenda DEVE combinar diretamente com o produto específico identificado no contexto V1.
- O nome do produto é APENAS uma referência interna para interpretação. NUNCA copie o nome do produto, nenhum trecho dele ou qualquer combinação de palavras dele para a legenda ou hashtags. Extraia mentalmente o tipo de item, uso, ambiente e característica mais evidente e expresse isso com palavras novas.
- O texto principal deve ser uma reação específica ao produto, evitando frases coringa que poderiam servir para qualquer item.\n- As hashtags devem ser específicas e diretamente relacionadas ao produto, categoria, uso, ambiente ou característica percebida; escolha naturalmente as hashtags mais adequadas ao contexto.\n- Antes de responder, confira internamente se frase, emoji e hashtags combinam semanticamente entre si e com o produto recebido.\n- Escolha exatamente UM emoji que naturalmente combine com a reação e com o contexto do produto. Evite emojis genéricos usados apenas como decoração e não transforme um emoji específico em assinatura repetida entre produtos.\n- Não explique seu raciocínio; retorne somente a legenda final.
- Se houver imagem, use-a apenas para reforçar a compreensão do produto já identificado pela V1; não invente outro produto.
- Texto principal: EXATAMENTE 2 ou 3 palavras.
- Exatamente 1 emoji.
- Segunda linha: exatamente 1 ou 2 hashtags relevantes ao contexto.
- NÃO escreva o nome literal do produto.
- NÃO escreva marca ou modelo.
- NÃO repita descrição, atributos ou características técnicas.
- NÃO revele quantidade, medidas, voltagem, embalagem ou termos comerciais.
- NÃO use linguagem de venda, urgência, promoção ou desconto.
- NÃO use: compre, comprar, garanta, aproveite, oferta, promoção, desconto,
  imperdível, corra ou equivalentes.
- Não use hashtags para contornar essas regras.
- Retorne somente as duas linhas finais, sem aspas e sem explicações.
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
        last_error = None

        for attempt in range(1, attempts + 1):
            try:
                response = self.requester(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                    json={
                        "contents": [{"parts": parts}],
                        "generationConfig": {"maxOutputTokens": 80},
                    },
                    timeout=int(os.getenv("ARMORED_CAPTION_API_TIMEOUT", "90")),
                )
                response.raise_for_status()
                data = response.json()
                try:
                    generated = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                except (KeyError, IndexError, TypeError) as exc:
                    raise CaptionGenerationError("Gemini não retornou texto de legenda") from exc

                try:
                    return validate_caption(
                        generated,
                        product_name=str(product.get("productName") or ""),
                        product_context=product,
                    )
                except CaptionPolicyError as exc:
                    raise CaptionGenerationError(
                        f"Gemini gerou legenda fora da política: {exc}"
                    ) from exc

            except (requests.RequestException, CaptionGenerationError) as exc:
                last_error = exc
                if attempt < attempts:
                    time.sleep(retry_delay)

        raise CaptionGenerationError(
            f"Gemini não conseguiu gerar uma legenda válida após "
            f"{attempts} tentativa(s): {last_error}"
        ) from last_error
