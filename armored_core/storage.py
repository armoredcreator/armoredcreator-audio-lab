from __future__ import annotations
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

def project_root() -> Path:
    configured = os.getenv("ARMORED_ROOT")
    return Path(configured).expanduser().resolve() if configured else Path(__file__).resolve().parents[1]

def affiliate_tail(url: str | None, fallback: str | None = None) -> str:
    value = ""
    if url:
        parsed = urlparse(str(url).strip())
        value = unquote(parsed.path.rstrip("/").split("/")[-1])
    if not value and fallback:
        value = str(fallback).strip()
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in value).strip("._-")
    return safe or "unknown"

class Storage:
    """Canonical filesystem projection.

    Telegram message ID is the operational/content identifier. The SQLite
    item_id remains an internal relational key only.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or project_root()).resolve()
        self.storage = self.root / "storage"
        self.database = self.storage / "database"
        self.videos = self.storage / "videos"
        self.logs = self.storage / "logs"
        self.backups = self.storage / "backups"
        for path in (self.database, self.videos, self.logs, self.backups):
            path.mkdir(parents=True, exist_ok=True)

    def source_workspace_root(self, source_id: str | int | None) -> Path:
        """Return the physical root assigned to one configured Telegram source."""
        value = str(source_id or "telegram").strip() or "telegram"
        index = None
        for candidate in range(1, 100):
            configured = (os.getenv(f"ARMORED_SOURCE_{candidate}_ID") or "").strip()
            configured_chat = (os.getenv(f"ARMORED_SOURCE_{candidate}_CHAT_ID") or "").strip()
            configured_key = (os.getenv(f"ARMORED_SOURCE_{candidate}_KEY") or "").strip()
            if value in {configured, configured_chat, configured_key}:
                index = candidate
                break
            if candidate > 1 and not configured and not configured_chat and not configured_key:
                break
        if index is None:
            match = __import__("re").fullmatch(r"source(\d+)", value, __import__("re").IGNORECASE)
            index = int(match.group(1)) if match else 1
        return self.storage / f"Videos GRUPO_FONTE_{index}"

    @staticmethod
    def _telegram_message_id(content_id: str | int, source_id: str | int | None = None) -> str:
        """Strip the source namespace from the internal SQLite key for disk names."""
        value = str(content_id).strip()
        source = str(source_id or "").strip()
        prefix = f"{source}_"
        if source and value.startswith(prefix):
            value = value[len(prefix):]
        if not value:
            raise ValueError("telegram-message-id-required")
        return value

    def workspace(self, telegram_message_id: str | int, source_id: str | int | None = None) -> Path:
        """Return the canonical source-separated workspace keyed by Telegram message ID."""
        message_id = self._telegram_message_id(telegram_message_id, source_id)
        path = self.source_workspace_root(source_id) / message_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def original(
        self,
        content_id: str,
        suffix: str = ".mp4",
        original_url: str | None = None,
        source_id: str | None = None,
    ) -> Path:
        message_id = self._telegram_message_id(content_id, source_id)
        return self.workspace(message_id, source_id=source_id) / f"{message_id}_finallinkoriginal{suffix}"

    def working(self, content_id: str, source_id: str | None = None) -> Path:
        message_id = self._telegram_message_id(content_id, source_id)
        return self.workspace(message_id, source_id=source_id) / f"{message_id}_.mp4"

    def result(
        self,
        content_id: str,
        affiliate_url: str | None = None,
        affiliate_name: str | None = None,
        source_id: str | None = None,
    ) -> Path:
        message_id = self._telegram_message_id(content_id, source_id)
        return self.workspace(message_id, source_id=source_id) / f"{message_id}_finaldomeulinknovo.mp4"
