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


def _tokens(value: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9]+", _fold(value)) if len(token) >= 4]


def _product_phrases(product_name: str) -> set[str]:
    stop = {
        "a", "as", "ao", "aos", "com", "da", "das", "de", "do", "dos", "e",
        "em", "para", "por", "sem", "um", "uma", "kit", "conjunto",
        "original", "novo", "nova",
    }
    tokens = [token for token in _tokens(product_name) if token not in stop]
    phrases: set[str] = set()
    for size in (2, 3):
        for index in range(0, max(0, len(tokens) - size + 1)):
            phrases.add(" ".join(tokens[index:index + size]))
    return phrases


def _hashtag_sequences(product_name: str) -> set[str]:
    stop = {
        "a", "as", "ao", "aos", "com", "da", "das", "de", "do", "dos", "e",
        "em", "para", "por", "sem", "um", "uma", "kit", "conjunto",
        "original", "novo", "nova",
    }
    tokens = [token for token in _tokens(product_name) if token not in stop]
    sequences: set[str] = set()
    for size in range(2, min(3, len(tokens)) + 1):
        for index in range(0, len(tokens) - size + 1):
            sequences.add("".join(tokens[index:index + size]))
    return sequences


def _brand_model_tokens(context: dict[str, Any] | None) -> set[str]:
    if not context:
        return set()
    leaked: set[str] = set()
    for key in ("brand", "brandName", "model", "modelName"):
        value = context.get(key)
        if value:
            leaked.update(_tokens(str(value)))
    return leaked


def _commercial_or_package_match(text: str) -> str | None:
    folded = _fold(text)
    for word in FORBIDDEN_WORDS:
        if re.search(rf"\b{re.escape(_fold(word))}\b", folded):
            return f"palavra proibida: {word}"
    for word in SALES_WORDS:
        folded_word = _fold(word)
        if re.search(rf"(?<![a-z0-9]){re.escape(folded_word)}(?![a-z0-9])", folded):
            return f"linguagem comercial proibida: {word}"
    return None


def _technical_leak(text: str) -> bool:
    folded = _fold(text)
    pattern = r"\b\d+(?:[\.,]\d+)?\s?(?:ml|l|litros?|cm|mm|m|kg|g|v|volts?|w|watts?)\b"
    return bool(re.search(pattern, folded))


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

    hard_error = _commercial_or_package_match(text)
    if hard_error:
        raise CaptionPolicyError(hard_error)

    main_text = HASHTAG_RE.sub("", text)
    main = re.sub(r"\s+", " ", main_text).strip()
    words = WORD_RE.findall(EMOJI_RE.sub("", main))
    if not 2 <= len(words) <= 3:
        raise CaptionPolicyError("texto principal deve conter 2 ou 3 palavras")

    if _technical_leak(text):
        raise CaptionPolicyError("legenda revela medida ou especificação técnica")

    folded_caption = _fold(EMOJI_RE.sub("", text))

    for phrase in _product_phrases(product_name):
        if re.search(rf"\b{re.escape(phrase)}\b", folded_caption):
            raise CaptionPolicyError("legenda copia expressão distintiva do produto")

    hashtag_tokens = {_fold(tag) for tag in hashtags}
    if hashtag_tokens & _hashtag_sequences(product_name):
        raise CaptionPolicyError("hashtag recompõe explicitamente o produto")

    if _brand_model_tokens(product_context) & set(re.findall(r"[a-z0-9]+", folded_caption)):
        raise CaptionPolicyError("legenda revela marca ou modelo do contexto V1")

    # Description/category overlap is deliberately allowed. With only 2–3
    # words available, rejecting ordinary contextual vocabulary causes false
    # negatives and unnecessary Gemini calls.
    return f"{main}\n{' '.join('#' + tag for tag in hashtags)}"
