from __future__ import annotations
import hashlib
import inspect
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol
from .database import Database
from .models import Item, PublicationCheck


class VisionUnresolvedError(RuntimeError):
    """Vision V1 could not resolve the product with enough certainty."""


class PublicationUnknownError(RuntimeError):
    """Telegram publication outcome is unresolved and must enter recovery.

    This is deliberately distinct from a normal pipeline failure: a timeout
    can happen after Telegram has accepted the upload, so the item must remain
    recoverable and must never be treated as a terminal FAILED item.
    """

from .storage import Storage

@dataclass(frozen=True)
class VisionResult:
    affiliate_name: str
    affiliate_url: str
    affiliate_urls: tuple[str, ...] = ()
    publication_caption: str | None = None
    ia_context: dict[str, Any] | None = None

@dataclass(frozen=True)
class StudioResult:
    working_path: Path | None
    result_path: Path

@dataclass(frozen=True)
class PublicationResult:
    confirmed: bool
    message_id: str | None

class VisionService(Protocol):
    def identify(self, item: Item) -> VisionResult: ...

class StudioService(Protocol):
    def process(self, item: Item) -> StudioResult: ...

class AIService(Protocol):
    def generate_caption(self, context: dict[str, Any]) -> str: ...

class Publisher(Protocol):
    def check_publication(self, item: Item) -> PublicationCheck: ...
    def publish(self, item: Item) -> PublicationResult: ...

@dataclass(frozen=True)
class IngestMessage:
    telegram_message_id: str
    source_id: str = "telegram"
    topic_id: int | None = None
    topic_name: str | None = None
    original_url: str | None = None
    source_path: Path | None = None
    materialize: Callable[[Path], None] | Callable[[Path], Awaitable[None]] | None = None

class SyncService:
    def __init__(self, db: Database, storage: Storage) -> None:
        self.db, self.storage = db, storage

    def _prepare_ingest(self, message: IngestMessage):
        if message.source_path is None and message.materialize is None:
            raise ValueError("ingest-message-requires-source-path-or-materializer")
        existing = self.db.conn.execute(
            "SELECT content_id, original_path, original_url FROM items WHERE source_id=? AND telegram_message_id=?",
            (message.source_id, message.telegram_message_id),
        ).fetchone()
        if existing:
            item_id = str(existing["content_id"])
            stored_path = str(existing["original_path"] or "").strip()
            if stored_path:
                original = Path(stored_path)
                if not original.name:
                    stored_path = ""
            if not stored_path:
                # Repair legacy rows deterministically from the stable Telegram ID + URL.
                original = self.storage.original(
                    message.telegram_message_id,
                    ".mp4",
                    original_url=existing["original_url"] or message.original_url,
                    source_id=message.source_id,
                )
                self.db.repair_original_path(item_id, original)
            partial = original.with_suffix(original.suffix + ".part")
            return item_id, original, partial
        suffix = (
            message.source_path.suffix
            if message.source_path is not None and message.source_path.suffix
            else ".mp4"
        )
        item_id = self.db.content_id_for(message.telegram_message_id, message.source_id)
        original = self.storage.original(message.telegram_message_id, suffix, original_url=message.original_url, source_id=message.source_id)
        self.db.reserve_item(
            message.telegram_message_id,
            source_id=message.source_id,
            topic_id=message.topic_id,
            topic_name=message.topic_name,
            original_url=message.original_url,
            original_path=original,
        )
        partial = original.with_suffix(original.suffix + ".part")
        return item_id, original, partial

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _finish_ingest(self, content_id: str, original: Path, partial: Path) -> int:
        if not partial.is_file() or partial.stat().st_size <= 0:
            raise IOError("original-materialization-empty")
        partial.replace(original)
        self.db.finalize_original_path(content_id, original, self._sha256(original))
        return content_id

    @staticmethod
    def _cleanup_ingest_files(original: Path, partial: Path) -> None:
        if partial.exists():
            partial.unlink()
        if original.exists():
            original.unlink()

    def reserve_message(self, message: IngestMessage) -> int:
        """Reserve one candidate in SQLite without downloading its media."""
        if message.source_path is None and message.materialize is None:
            raise ValueError("ingest-message-requires-source-path-or-materializer")
        existing = self.db.conn.execute(
            "SELECT content_id, original_path, original_url FROM items WHERE source_id=? AND telegram_message_id=?",
            (message.source_id, message.telegram_message_id),
        ).fetchone()
        if existing:
            item_id = str(existing["content_id"])
            if not str(existing["original_path"] or "").strip():
                original = self.storage.original(message.telegram_message_id, ".mp4", original_url=existing["original_url"] or message.original_url, source_id=message.source_id)
                self.db.repair_original_path(item_id, original)
            return item_id
        suffix = message.source_path.suffix if message.source_path is not None and message.source_path.suffix else ".mp4"
        item_id = self.db.content_id_for(message.telegram_message_id, message.source_id)
        original = self.storage.original(item_id, suffix, original_url=message.original_url, source_id=message.source_id)
        self.db.reserve_item(message.telegram_message_id, source_id=message.source_id, topic_id=message.topic_id,
                             topic_name=message.topic_name, original_url=message.original_url, original_path=original)
        return item_id

    async def materialize_message_async(self, message: IngestMessage) -> int:
        """Materialize an already-reserved candidate into its immutable path."""
        item_id, original, partial = self._prepare_ingest(message)
        if original.exists():
            return item_id
        try:
            if partial.exists():
                partial.unlink()
            if message.materialize is not None:
                result = message.materialize(partial)
                if inspect.isawaitable(result):
                    await result
            else:
                source = message.source_path.resolve()
                if not source.is_file():
                    raise FileNotFoundError(source)
                shutil.copy2(source, partial)
            return self._finish_ingest(item_id, original, partial)
        except Exception:
            self._cleanup_ingest_files(original, partial)
            self.db.rollback_ingest()
            raise

    def ingest_message(self, message: IngestMessage) -> int:
        item_id, original, partial = self._prepare_ingest(message)
        if original.exists():
            return item_id
        try:
            if partial.exists():
                partial.unlink()
            if message.materialize is not None:
                result = message.materialize(partial)
                if inspect.isawaitable(result):
                    raise TypeError("async-materializer-requires-ingest-message-async")
            else:
                source = message.source_path.resolve()
                if not source.is_file():
                    raise FileNotFoundError(source)
                shutil.copy2(source, partial)
            return self._finish_ingest(item_id, original, partial)
        except Exception:
            self._cleanup_ingest_files(original, partial)
            self.db.rollback_ingest()
            raise

    async def ingest_message_async(self, message: IngestMessage) -> int:
        item_id, original, partial = self._prepare_ingest(message)
        if original.exists():
            return item_id
        try:
            if partial.exists():
                partial.unlink()
            if message.materialize is not None:
                result = message.materialize(partial)
                if inspect.isawaitable(result):
                    await result
            else:
                source = message.source_path.resolve()
                if not source.is_file():
                    raise FileNotFoundError(source)
                shutil.copy2(source, partial)
            return self._finish_ingest(item_id, original, partial)
        except Exception:
            self._cleanup_ingest_files(original, partial)
            self.db.rollback_ingest()
            raise

    def ingest(
        self,
        source: Path,
        telegram_message_id: str,
        source_id: str = "telegram",
        topic_id: int | None = None,
        topic_name: str | None = None,
        original_url: str | None = None,
    ) -> int:
        return self.ingest_message(
            IngestMessage(
                telegram_message_id=telegram_message_id,
                source_id=source_id,
                topic_id=topic_id,
                topic_name=topic_name,
                original_url=original_url,
                source_path=Path(source),
            )
        )
