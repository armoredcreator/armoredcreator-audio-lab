from __future__ import annotations

import os
import re
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

def source_slug(source_id: str | None) -> str:
    value = str(source_id or "").strip()
    if not value:
        return "legacy"
    if value == "telegram":
        return "legacy"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip(".")
    return safe or "legacy"

class Storage:
    """Canonical filesystem projection with source-isolated media workspaces.

    SQLite remains shared because it is the source of truth. Physical media,
    derived artifacts and per-source operational logs live under
    storage/sources/{source_id}.

    source_id=None preserves the legacy storage/videos layout.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or project_root()).resolve()
        self.storage = self.root / "storage"
        self.database = self.storage / "database"
        self.videos = self.storage / "videos"
        self.logs = self.storage / "logs"
        self.backups = self.storage / "backups"
        self.sources = self.storage / "sources"
        for path in (self.database, self.videos, self.logs, self.backups, self.sources):
            path.mkdir(parents=True, exist_ok=True)

    def _media_root(self, source_id: str | None = None) -> Path:
        # The historical single-source Telegram contract is canonical under
        # storage/videos. Real multi-source IDs get physically isolated roots
        # under storage/sources/{source_id}.
        if source_id is None or str(source_id).strip() == "telegram":
            return self.storage
        return self.sources / source_slug(source_id)

    def _videos_root(self, source_id: str | None = None) -> Path:
        path = self._media_root(source_id) / "videos"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def workspace(self, telegram_message_id: str | int, source_id: str | None = None) -> Path:
        content_id = str(telegram_message_id).strip()
        if not content_id:
            raise ValueError("telegram-message-id-required")
        path = self._videos_root(source_id) / content_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def original(self, content_id: str, suffix: str = ".mp4", original_url: str | None = None, source_id: str | None = None) -> Path:
        content_id = str(content_id).strip()
        if not content_id:
            raise ValueError("content-id-required")
        tail = affiliate_tail(original_url)
        return self.workspace(content_id, source_id=source_id) / f"{content_id}_{tail}{suffix}"

    def working(self, content_id: str, source_id: str | None = None) -> Path:
        content_id = str(content_id).strip()
        if not content_id:
            raise ValueError("content-id-required")
        return self.workspace(content_id, source_id=source_id) / f"{content_id}_.mp4"

    def result(self, content_id: str, affiliate_url: str | None = None, affiliate_name: str | None = None, source_id: str | None = None) -> Path:
        content_id = str(content_id).strip()
        if not content_id:
            raise ValueError("content-id-required")
        tail = affiliate_tail(affiliate_url, affiliate_name)
        return self.workspace(content_id, source_id=source_id) / f"{content_id}_{tail}.mp4"