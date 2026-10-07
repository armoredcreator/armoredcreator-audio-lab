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
    """Canonical filesystem projection with one identical workspace per source.

    SQLite remains shared because it is the source of truth. Real Telegram
    sources are isolated by their configured video-root folder directly under
    storage; there is deliberately no storage/sources/ hierarchy.

    The historical single-source test/default contract keeps storage/videos
    when no source-specific root is configured.
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

    def _configured_source_video_root(self, source_id: str) -> Path | None:
        source_id = str(source_id).strip()
        for index in range(1, 10):
            configured_id = (
                os.getenv(f"ARMORED_SOURCE_{index}_ID")
                or os.getenv(f"ARMORED_SOURCE_{index}_CHAT_ID")
                or ""
            ).strip()
            if not configured_id or configured_id != source_id:
                continue
            folder = (
                os.getenv(f"ARMORED_SOURCE_{index}_VIDEO_DIR")
                or f"Videos GRUPO_FONTE_{index}"
            ).strip()
            return self.storage / folder
        return None

    def _videos_root(self, source_id: str | None = None) -> Path:
        normalized = str(source_id or "").strip()
        if not normalized or normalized == "telegram":
            path = self.videos
        else:
            # Real configured sources get their own top-level video folder.
            # Unconfigured synthetic/test sources retain the legacy root and
            # do not create an invented storage hierarchy.
            path = self._configured_source_video_root(normalized) or self.videos
        path.mkdir(parents=True, exist_ok=True)
        return path

    def video_roots(self) -> tuple[Path, ...]:
        """Return every configured video root without creating directories."""
        roots: list[Path] = [self.videos]
        for index in range(1, 10):
            source_id = (
                os.getenv(f"ARMORED_SOURCE_{index}_ID")
                or os.getenv(f"ARMORED_SOURCE_{index}_CHAT_ID")
                or ""
            ).strip()
            if not source_id:
                continue
            folder = (
                os.getenv(f"ARMORED_SOURCE_{index}_VIDEO_DIR")
                or f"Videos GRUPO_FONTE_{index}"
            ).strip()
            path = self.storage / folder
            if path not in roots:
                roots.append(path)
        return tuple(roots)

    def workspace_path(
        self,
        telegram_message_id: str | int,
        source_id: str | None = None,
    ) -> Path:
        content_id = str(telegram_message_id).strip()
        if not content_id:
            raise ValueError("telegram-message-id-required")
        normalized = str(source_id or "").strip()
        if not normalized or normalized == "telegram":
            return self.videos / content_id
        root = self._configured_source_video_root(normalized) or self.videos
        return root / content_id

    def workspace(self, telegram_message_id: str | int, source_id: str | None = None) -> Path:
        path = self.workspace_path(telegram_message_id, source_id=source_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def original(
        self,
        content_id: str,
        suffix: str = ".mp4",
        original_url: str | None = None,
        source_id: str | None = None,
    ) -> Path:
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

    def result(
        self,
        content_id: str,
        affiliate_url: str | None = None,
        affiliate_name: str | None = None,
        source_id: str | None = None,
    ) -> Path:
        content_id = str(content_id).strip()
        if not content_id:
            raise ValueError("content-id-required")
        tail = affiliate_tail(affiliate_url, affiliate_name)
        return self.workspace(content_id, source_id=source_id) / f"{content_id}_{tail}.mp4"
