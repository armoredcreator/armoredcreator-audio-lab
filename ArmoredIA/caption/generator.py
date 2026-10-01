from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any

from ..providers.base import AIProvider
from ..providers.gemini import GeminiProvider
from .selector import (
    CaptionCandidateEvaluation,
    CaptionGenerationError,
    CaptionSelection,
    CaptionSelector,
)


@dataclass(frozen=True)
class CaptionGenerationResult:
    caption: str
    selection: CaptionSelection
    evaluations: tuple[CaptionCandidateEvaluation, ...]
    batch_id: str


class CaptionGenerator:
    """One AI-provider request -> all candidates -> local Policy/ranking."""

    def __init__(self, provider: AIProvider | None = None, selector: CaptionSelector | None = None):
        self.provider = provider or GeminiProvider()
        self.selector = selector or CaptionSelector()

    def generate_with_evidence(
        self,
        product_context: dict[str, Any],
    ) -> CaptionGenerationResult:
        if os.getenv("ARMORED_IA_CAPTION_ENABLED", "1") != "1":
            raise CaptionGenerationError("gerador de legenda desativado")

        model_limit = max(1, int(os.getenv("ARMORED_IA_MAX_CANDIDATES", "10")))
        batch_id = uuid.uuid4().hex
        candidates = self.provider.generate_candidates(
            "caption",
            dict(product_context),
            min(model_limit, 10),
        )
        evaluations = self.selector.evaluate(
            candidates,
            product_name=str(product_context.get("productName") or ""),
            product_context=product_context,
        )
        try:
            selection, marked = self.selector.choose(evaluations)
        except CaptionGenerationError as exc:
            raise CaptionGenerationError(
                str(exc),
                batch_id=batch_id,
                evaluations=evaluations,
            ) from exc
        return CaptionGenerationResult(
            caption=selection.caption,
            selection=selection,
            evaluations=marked,
            batch_id=batch_id,
        )

    def generate(self, product_context: dict[str, Any]) -> str:
        return self.generate_with_evidence(product_context).caption
