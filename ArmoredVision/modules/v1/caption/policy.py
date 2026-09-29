from __future__ import annotations

import re
import unicodedata
from typing import Any

FORBIDDEN_WORDS = {"embalagem", "tampa", "frasco", "lacre"}
SALES_WORDS = {
    "compre", "comprar", "garanta", "garantir", "imperdivel", "imperdível",
    "aproveite", "oferta", "ofertas", "promocao", "promoção", "desconto",
    "corra", "nao perca", "não perca",
}
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF]")
HASHTAG_RE = re.compile(r"(?<!\w)#([\wÀ-ÿ]+)", re.UNICODE)
WORD_RE = re.compile(r"[A-Za-zÀ-ÿ0-9]+(?:['-][A-Za-zÀ-ÿ0-9]+)?", re.UNICODE)


class CaptionPolicyError(ValueError):
    pass


def _fold(text: str) -> str:
    value = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in value if not unicodedata.combining(ch)).casefold()


def _meaningful_product_tokens(product_name: str) -> set[str]:
    stop = {
        "a", "as", "ao", "aos", "com", "da", "das", "de", "do", "dos", "e",
        "em", "para", "por", "sem", "um", "uma", "kit", "conjunto",
        "original", "novo", "nova",
    }
    return {
        token for token in re.findall(r"[a-z0-9]+", _fold(product_name))
        if token not in stop and len(token) >= 4
    }


def _meaningful_product_token_sequences(product_name: str) -> set[str]:
    """Return bounded contiguous token phrases used by hashtags.
 
    Short contiguous phrases catch hashtag recombinations such as
    ``batommatte`` without generating exponential token subsets.
    """
    stop = {
        "a", "as", "ao", "aos", "com", "da", "das", "de", "do", "dos", "e",
        "em", "para", "por", "sem", "um", "uma", "kit", "conjunto",
        "original", "novo", "nova",
    }
    tokens = [
        token for token in re.findall(r"[a-z0-9]+", _fold(product_name))
        if token not in stop and len(token) >= 4
    ]
    sequences: set[str] = set()
    max_size = min(3, len(tokens))
    for size in range(2, max_size + 1):
        for index in range(0, len(tokens) - size + 1):
            sequences.add("".join(tokens[index:index + size]))
    return sequences

def _meaningful_product_phrases(product_name: str) -> set[str]:
    """Return short contiguous product-name phrases for caption leak checks."""
    stop = {
        "a", "as", "ao", "aos", "com", "da", "das", "de", "do", "dos", "e",
        "em", "para", "por", "sem", "um", "uma", "kit", "conjunto",
        "original", "novo", "nova",
    }
    tokens = [
        token for token in re.findall(r"[a-z0-9]+", _fold(product_name))
        if token not in stop and len(token) >= 4
    ]
    phrases: set[str] = set()
    for size in (2, 3):
        for index in range(0, max(0, len(tokens) - size + 1)):
            phrases.add(" ".join(tokens[index:index + size]))
    return phrases


def _context_leak_tokens(product_context: dict[str, Any] | None) -> set[str]:
    if not product_context:
        return set()
    leaked: set[str] = set()
    for key in ("brand", "brandName", "model", "modelName"):
        value = product_context.get(key)
        if value:
            leaked.update(
                token for token in re.findall(r"[a-z0-9]+", _fold(value))
                if len(token) >= 4
            )
    return leaked


def _context_leak_phrases(product_context: dict[str, Any] | None) -> set[str]:
    if not product_context:
        return set()
    phrases: set[str] = set()
    for key in (
        "model",
        "modelName",
        "description",
        "technicalCharacteristics",
    ):
        value = product_context.get(key)
        if not isinstance(value, str):
            continue
        tokens = [
            token for token in re.findall(r"[a-z0-9]+", _fold(value))
            if key in ("model", "modelName") or len(token) >= 4
        ]
        for size in (2, 3):
            phrases.update(
                " ".join(tokens[index:index + size])
                for index in range(0, max(0, len(tokens) - size + 1))
            )
    return phrases


def validate_caption(
    caption: str,
    *,
    product_name: str = "",
    product_context: dict[str, Any] | None = None,
) -> str:
    text = str(caption or "").replace("\r", "").strip()
    text = re.sub(r"^[*_~\s]+|[*_~\s]+$", "", text)
    if not text:
        raise CaptionPolicyError("legenda vazia")

    hashtags = HASHTAG_RE.findall(text)
    if not 1 <= len(hashtags) <= 2:
        raise CaptionPolicyError("a legenda precisa ter 1 ou 2 hashtags")
    if any(len(tag) > 20 for tag in hashtags):
        raise CaptionPolicyError("hashtag longa demais")
    if len(EMOJI_RE.findall(text)) != 1:
        raise CaptionPolicyError("a legenda precisa ter exatamente 1 emoji")

    folded = _fold(text)
    for forbidden in FORBIDDEN_WORDS:
        if re.search(rf"\b{re.escape(_fold(forbidden))}\b", folded):
            raise CaptionPolicyError(f"palavra proibida: {forbidden}")
    for sales_word in SALES_WORDS:
        if _fold(sales_word) in folded:
            raise CaptionPolicyError(f"linguagem comercial proibida: {sales_word}")

    # Gemini may return hashtags on the same line or on a separate line.
    # Hashtag placement is presentation, not a policy rule. Remove the
    # validated hashtags first, then validate the remaining text as exactly
    # one main line.
    main_text = HASHTAG_RE.sub("", text)
    main_lines = [
        re.sub(r"\\s+", " ", line).strip()
        for line in main_text.splitlines()
        if line.strip()
    ]
    if len(main_lines) != 1:
        raise CaptionPolicyError("texto principal deve ocupar uma única linha")

    main = main_lines[0]
    words = WORD_RE.findall(EMOJI_RE.sub("", main))
    if not 2 <= len(words) <= 3:
        raise CaptionPolicyError("texto principal deve conter 2 ou 3 palavras")

    caption_tokens = set(re.findall(r"[a-z0-9]+", _fold(EMOJI_RE.sub("", text))))

    # A generic product-type word is allowed in a natural reaction
    # ("Essa bolsa 😍", "Que tênis lindo 👟"). What remains blocked is a
    # distinctive multi-word reproduction of the V1 product title.
    product_phrases = _meaningful_product_phrases(product_name)
    caption_words = _fold(EMOJI_RE.sub("", text))
    if any(
        phrase and re.search(rf"\b{re.escape(phrase)}\b", caption_words)
        for phrase in product_phrases
    ):
        raise CaptionPolicyError("legenda copia expressão distintiva do produto")

    product_sequences = _meaningful_product_token_sequences(product_name)
    hashtag_tokens = {_fold(tag) for tag in hashtags}
    if hashtag_tokens & product_sequences:
        raise CaptionPolicyError("hashtag recompõe explicitamente o produto")

    context_tokens = _context_leak_tokens(product_context)
    if caption_tokens & context_tokens:
        raise CaptionPolicyError("legenda revela marca ou modelo do contexto V1")

    context_phrases = _context_leak_phrases(product_context)
    folded_caption = _fold(EMOJI_RE.sub("", text))
    if any(phrase in folded_caption for phrase in context_phrases):
        raise CaptionPolicyError("legenda repete trecho da descrição do contexto V1")

    return f"{main}\n{' '.join('#' + tag for tag in hashtags)}"
