from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .policy import CaptionPolicyError, validate_caption


class CaptionGenerationError(RuntimeError):
    """Gemini returned candidates, but no candidate survived local Policy."""


@dataclass(frozen=True)
class CaptionSelection:
    caption: str
    index: int


class CaptionSelector:
    def select(
        self,
        candidates: list[str],
        *,
        product_name: str = "",
        product_context: dict[str, Any] | None = None,
    ) -> CaptionSelection:
        reasons: list[str] = []
        for index, candidate in enumerate(candidates[:10], start=1):
            try:
                caption = validate_caption(
                    candidate,
                    product_name=product_name,
                    product_context=product_context,
                )
            except CaptionPolicyError as exc:
                reasons.append(f"{index}: {exc}")
                continue
            return CaptionSelection(caption, index)

        detail = "; ".join(reasons[:10]) or "nenhuma candidata retornada"
        raise CaptionGenerationError(
            f"Nenhuma das {len(candidates[:10])} candidata(s) passou pela Policy: {detail}"
        )
