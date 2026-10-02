from __future__ import annotations

from typing import Any, Protocol


class AIProviderError(RuntimeError):
    """Provider transport, availability, or malformed-response failure."""


class AIProvider(Protocol):
    def generate_candidates(self, task: str, context: dict[str, Any], limit: int) -> list[str]:
        """Return model candidates for one AI task."""
