from __future__ import annotations

import os
from typing import Any

from .caption.generator import CaptionGenerator


class ArmoredIA:
    """Single AI boundary; task dispatch is extensible without changing Pipeline."""

    def __init__(self, caption_generator: CaptionGenerator | None = None):
        self.caption_generator = caption_generator or CaptionGenerator()

    def generate_caption(self, product_context: dict[str, Any]) -> str:
        return self.caption_generator.generate(product_context)

    def run(self, task: str, context: dict[str, Any]) -> Any:
        task_name = str(task or "").strip().casefold()
        if task_name == "caption":
            if os.getenv("ARMORED_IA_ENABLED", "1") != "1":
                raise RuntimeError("ArmoredIA desativado")
            return self.generate_caption(context)
        raise ValueError(f"ArmoredIA task não suportada: {task}")

    def build(self, **_kwargs):
        return self


def build(**_kwargs):
    return ArmoredIA()
