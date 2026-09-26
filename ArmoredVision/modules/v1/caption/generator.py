from __future__ import annotations

import base64
import os
import time
from typing import Any, Callable

import requests

from .policy import CaptionPolicyError, validate_caption

DEFAULT_REACTIONS = (
    ("beleza", ("Olha esse charme ✨", ("#autocuidado", "#rotina"))),
    ("maqui", ("Fiquei encantada 😍", ("#autocuidado", "#rotina"))),
    ("casa", ("Que achado lindo ✨", ("#decoracao", "#rotina"))),
    ("decor", ("Que charme aqui ✨", ("#decoracao", "#rotina"))),
    ("moda", ("Olha esse look ✨", ("#estilo", "#rotina"))),
    ("cozinha", ("Olha que pratico ✨", ("#casa", "#rotina"))),
)


class CaptionGenerationError(RuntimeError):
    pass


def _deterministic_caption(product: dict[str, Any]) -> str:
    context = str(product.get("category_name") or product.get("category") or "").casefold()
    product_name = str(product.get("productName") or "")
    candidates: list[str] = []

    for key, (main, tags) in DEFAULT_REACTIONS:
        if key in context:
            candidates.append(main + "\n" + " ".join(tags))

    candidates.append("Olha esse charme ✨\n#achadinhos #rotina")

    for candidate in candidates:
        try:
            return validate_caption(candidate, product_name=product_name, product_context=product)
        except CaptionPolicyError:
            continue

    raise CaptionGenerationError("nenhum fallback determinístico passou pela política")


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
    """Optional Gemini generator with a deterministic operational fallback."""

    def __init__(self, requester: Callable[..., Any] | None = None):
        self.requester = requester or requests.post

    def _fallback(self, product: dict[str, Any]) -> str:
        if os.getenv("ARMORED_CAPTION_ALLOW_DETERMINISTIC_FALLBACK", "1") != "1":
            raise CaptionGenerationError("fallback determinístico desativado")
        return validate_caption(
            _deterministic_caption(product),
            product_name=str(product.get("productName") or ""),
            product_context=product,
        )

    def generate(self, product: dict[str, Any]) -> str:
        if os.getenv("ARMORED_CAPTION_ENABLED", "0") != "1":
            raise CaptionGenerationError("gerador de legenda desativado")

        api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
        if not api_key:
            return self._fallback(product)

        model = os.getenv("ARMORED_CAPTION_MODEL", "gemini-3.1-flash-lite")
        prompt = """
Você recebe contexto de um produto que JÁ FOI identificado pela Vision V1.
A identidade definida pela Vision V1 é a fonte de verdade. Você NÃO deve
identificar, corrigir, substituir ou renomear o produto.

Sua tarefa é apenas interpretar o contexto e criar uma reação curta e natural
para um vídeo de descoberta. Pode se inspirar em aparência, estilo, acabamento,
utilidade percebida ou sensação visual, mas NÃO transforme a legenda em
descrição de catálogo.

REGRAS:
- Português do Brasil.
- A legenda DEVE combinar diretamente com o produto específico identificado no contexto V1.
- O nome do produto é APENAS uma referência interna para interpretação. NUNCA copie o nome do produto, nenhum trecho dele ou qualquer combinação de palavras dele para a legenda ou hashtags. Extraia mentalmente o tipo de item, uso, ambiente e característica mais evidente e expresse isso com palavras novas.
- O texto principal deve transmitir uma reação que só faça sentido para aquele tipo de produto, evitando frases coringa que serviriam para qualquer item.
- As hashtags também DEVEM ser específicas e diretamente relacionadas ao produto, à sua categoria, ao uso ou ao ambiente em que ele é usado; NÃO use hashtags genéricas como #achadinhos, #rotina ou #casa quando houver uma opção mais específica.
- Antes de responder, confira internamente se frase, emoji e hashtags combinam SEMANTICAMENTE entre si e com o produto recebido.
- O emoji deve reforçar diretamente o significado da frase e ter relação clara com o produto, seu uso, sua categoria ou seu contexto. NÃO use emoji apenas como decoração aleatória.
- A frase, o emoji e as hashtags devem formar uma única reação coerente ao produto específico. Se trocar a frase, reavalie também o emoji e as hashtags.
- Não explique seu raciocínio; retorne somente a legenda final.
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

        if os.getenv("ARMORED_CAPTION_ALLOW_DETERMINISTIC_FALLBACK", "1") == "1":
            return self._fallback(product)

        raise CaptionGenerationError(
            f"Gemini não conseguiu gerar uma legenda válida após "
            f"{attempts} tentativa(s): {last_error}"
        ) from last_error
