from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from .policy import CaptionPolicyError, validate_caption


class CaptionGenerationError(RuntimeError):
    """Gemini/Policy could not produce a usable caption."""

    def __init__(
        self,
        message: str,
        *,
        batch_id: str | None = None,
        evaluations: tuple["CaptionCandidateEvaluation", ...] = (),
    ):
        super().__init__(message)
        self.batch_id = batch_id
        self.evaluations = evaluations


@dataclass(frozen=True)
class CaptionCandidateEvaluation:
    index: int
    caption: str
    policy_valid: bool
    rejection_reason: str | None
    score: float
    selected: bool = False


@dataclass(frozen=True)
class CaptionSelection:
    caption: str
    index: int
    score: float


def _context_tokens(product_context: dict[str, Any] | None) -> set[str]:
    if not product_context:
        return set()
    ignored = {
        "productName", "itemId", "shopId", "shopName",
        "priceMin", "priceMax", "sales", "ratingStar",
        "brand", "brandName", "model", "modelName",
        "imageUrl",
    }
    tokens: set[str] = set()
    for key, value in product_context.items():
        if key in ignored or value in (None, "", [], {}):
            continue
        for token in re.findall(r"[a-z0-9]+", str(value).casefold()):
            if len(token) >= 4:
                tokens.add(token)
    return tokens


def _caption_score(
    caption: str,
    *,
    product_context: dict[str, Any] | None,
) -> float:
    context_tokens = _context_tokens(product_context)
    main = re.sub(r"#[\wÀ-ÿ]+", "", caption, flags=re.UNICODE)
    main_tokens = [token.casefold() for token in re.findall(r"[A-Za-zÀ-ÿ0-9]+", main)]
    hashtag_tokens = [
        token.casefold()
        for token in re.findall(r"#([\wÀ-ÿ]+)", caption, flags=re.UNICODE)
    ]

    score = 0.0
    score += 2.0 * sum(token in context_tokens for token in main_tokens)
    score += 1.0 * sum(token in context_tokens for token in hashtag_tokens)

    # Prefer a slightly richer 3-word reaction when relevance is otherwise tied.
    if len(main_tokens) == 3:
        score += 0.25

    # Penalize repetition inside the tiny caption surface.
    score -= 0.25 * (len(main_tokens) - len(set(main_tokens)))
    return round(score, 4)


class CaptionSelector:
    def evaluate(
        self,
        candidates: list[str],
        *,
        product_name: str = "",
        product_context: dict[str, Any] | None = None,
    ) -> tuple[CaptionCandidateEvaluation, ...]:
        evaluations: list[CaptionCandidateEvaluation] = []
        for index, candidate in enumerate(candidates[:10], start=1):
            raw = str(candidate or "").strip()
            try:
                caption = validate_caption(
                    raw,
                    product_name=product_name,
                    product_context=product_context,
                )
            except CaptionPolicyError as exc:
                evaluations.append(
                    CaptionCandidateEvaluation(
                        index=index,
                        caption=raw,
                        policy_valid=False,
                        rejection_reason=str(exc),
                        score=0.0,
                    )
                )
                continue

            evaluations.append(
                CaptionCandidateEvaluation(
                    index=index,
                    caption=caption,
                    policy_valid=True,
                    rejection_reason=None,
                    score=_caption_score(
                        caption,
                        product_context=product_context,
                    ),
                )
            )
        return tuple(evaluations)

    def choose(
        self,
        evaluations: tuple[CaptionCandidateEvaluation, ...],
    ) -> tuple[CaptionSelection, tuple[CaptionCandidateEvaluation, ...]]:
        valid = [evaluation for evaluation in evaluations if evaluation.policy_valid]
        if not valid:
            detail = "; ".join(
                f"{evaluation.index}: {evaluation.rejection_reason}"
                for evaluation in evaluations
                if evaluation.rejection_reason
            ) or "nenhuma candidata retornada"
            raise CaptionGenerationError(
                f"Nenhuma das {len(evaluations)} candidata(s) passou pela Policy: {detail}",
            )

        # Deterministic ranking: highest local relevance score, then the
        # earliest model index only as an explicit tie-breaker.
        best = max(valid, key=lambda evaluation: (evaluation.score, -evaluation.index))
        marked = tuple(
            CaptionCandidateEvaluation(
                index=evaluation.index,
                caption=evaluation.caption,
                policy_valid=evaluation.policy_valid,
                rejection_reason=evaluation.rejection_reason,
                score=evaluation.score,
                selected=evaluation.index == best.index,
            )
            for evaluation in evaluations
        )
        return (
            CaptionSelection(
                caption=best.caption,
                index=best.index,
                score=best.score,
            ),
            marked,
        )

    def select(
        self,
        candidates: list[str],
        *,
        product_name: str = "",
        product_context: dict[str, Any] | None = None,
    ) -> CaptionSelection:
        evaluations = self.evaluate(
            candidates,
            product_name=product_name,
            product_context=product_context,
        )
        selection, _ = self.choose(evaluations)
        return selection
