from __future__ import annotations

import os
from typing import Any

from ..providers.base import AIProvider, AIProviderError
from ..providers.gemini import GeminiProvider
from .selector import CaptionGenerationError, CaptionSelector


class CaptionGenerator:
    """One AI-provider request -> candidates -> local Policy selection."""

    def __init__(self, provider: AIProvider | None = None, selector: CaptionSelector | None = None):
        self.provider = provider or GeminiProvider()
        self.selector = selector or CaptionSelector()

    def generate(self, product_context: dict[str, Any]) -> str:
        if os.getenv("ARMORED_IA_CAPTION_ENABLED", "1") != "1":
            raise CaptionGenerationError("gerador de legenda desativado")

        model_limit = max(1, int(os.getenv("ARMORED_IA_MAX_CANDIDATES", "10")))
        candidates = self.provider.generate_candidates(
            "caption",
            dict(product_context),
            min(model_limit, 10),
        )
        return self.selector.select(
            candidates,
            product_name=str(product_context.get("productName") or ""),
            product_context=product_context,
        ).caption
