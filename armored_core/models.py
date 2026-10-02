from __future__ import annotations
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

class State(StrEnum):
    RECEIVED = "RECEIVED"
    VISION = "VISION"
    WAITING_VISION = "WAITING_VISION"
    IA = "IA"
    STUDIO = "STUDIO"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    RECOVERY = "RECOVERY"
    FAILED = "FAILED"

class PublicationCheck(StrEnum):
    CONFIRMED = "CONFIRMED"
    ABSENT = "ABSENT"
    UNKNOWN = "UNKNOWN"

@dataclass(frozen=True)
class Item:
    content_id: str
    telegram_message_id: str
    state: State
    workspace: Path
    original_path: Path
    working_path: Path | None
    result_path: Path | None
    affiliate_name: str | None
    affiliate_url: str | None
    source_id: str = "telegram"
    original_url: str | None = None
    topic_id: int | None = None
    topic_name: str | None = None
    original_sha256: str | None = None
    attempts: int = 0
    recovery_count: int = 0
    cleanup_completed: bool = False
    publication_caption: str | None = None
    affiliate_urls: tuple[str, ...] = ()
    ia_context: dict | None = None

    @property
    def item_id(self) -> str:
        """Backward-compatible alias; content_id is the canonical identity."""
        return self.content_id
