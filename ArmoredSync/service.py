from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from armored_core.database import Database
from armored_core.services import IngestMessage, SyncService


@dataclass(frozen=True)
class SyncMessage:
    telegram_message_id: str
    source_id: str = "telegram"
    topic_id: int | None = None
    topic_name: str | None = None
    original_url: str | None = None
    source_path: Path | None = None
    materialize: Any | None = None


class LocalSource:
    """Deterministic offline source used by the lab and E2E tests."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._seen: set[str] = set()

    def fetch_next(self) -> SyncMessage | None:
        self.root.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.root.iterdir()):
            if not path.is_file() or path.suffix.lower() not in {".mp4", ".mov", ".mkv"}:
                continue
            message_id = path.stem
            if message_id in self._seen:
                continue
            self._seen.add(message_id)
            return SyncMessage(
                telegram_message_id=message_id,
                source_id="local",
                original_url=os.getenv("ARMORED_TEST_ORIGINAL_URL"),
                source_path=path,
            )
        return None


class TelegramReader:
    """Small path-safe Telethon adapter owned by ArmoredSync."""

    def __init__(self, root: Path, api_id: int, api_hash: str):
        try:
            from telethon import TelegramClient
        except ImportError as exc:
            raise RuntimeError("Dependência Telethon ausente; instale as dependências do Sync.") from exc

        self._session = Path(root) / "credentials" / "telegram" / "session" / "armoredsync"
        self._api_id = api_id
        self._api_hash = api_hash
        self._TelegramClient = TelegramClient
        self._build_client()

    def _build_client(self) -> None:
        self._session.parent.mkdir(parents=True, exist_ok=True)
        self.client = self._TelegramClient(str(self._session), self._api_id, self._api_hash)

    async def _close_client(self) -> None:
        """Release every Telethon resource before replacing the client.

        A failed connection can leave the SQLiteSession open even when
        ``is_connected()`` is already false. Replacing that client object
        without closing it can make the next reconnect fail with
        ``sqlite3.OperationalError: database is locked`` on Windows.
        """
        client = getattr(self, "client", None)
        if client is None:
            return

        try:
            await client.disconnect()
        except Exception:
            session = getattr(client, "session", None)
            close = getattr(session, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        else:
            session = getattr(client, "session", None)
            close = getattr(session, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    async def connect(self):
        # Telethon binds a client to the event loop used by its first
        # connection. Always close the previous client before rebuilding it,
        # especially after a failed connect, so the SQLite session is never
        # owned by two client objects in the same process.
        if self.client.is_connected():
            return

        await self._close_client()
        self._build_client()
        try:
            await self.client.start()
        except Exception:
            await self._close_client()
            raise
        else:
            import logging
            logging.getLogger(__name__).info(
                "[SYNC][TELEGRAM] sessão Telegram conectada/reconectada"
            )

    async def disconnect(self):
        await self._close_client()

    def get_messages(self, source, limit=None):
        return self.client.iter_messages(source, limit=limit)


class TelegramSource:
    """Fonte real do ArmoredSync.

    Importante: o Sync não baixa para uma pasta própria. Ele somente descobre
    o candidato e entrega ao Core um materializer que grava diretamente no
    workspace canônico storage/videos/{item_id}.
    """

    URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
    SHOPEE_DOMAINS = ("shopee.com.br", "shopee.co", "shopee.ee")
    # Class defaults preserve legacy tests that instantiate TelegramSource via __new__.
    source: str | None = None
    source_id: str | None = None

    def __init__(
        self,
        root: Path,
        reader: Any,
        db: Database | None = None,
        source: str | None = None,
        source_id: str | None = None,
    ):
        self.root = Path(root)
        self.reader = reader
        self.db = db
        self.source = str(source).strip() if source is not None else None
        self.source_id = str(source_id).strip() if source_id is not None else None
        self._seen: set[int] = set()
        self._topic_iterator = None
        self._topics: list[tuple[int, str]] | None = None
        self._historical_complete = False
        self._historical_checkpoints: dict[int, int] = {}
        self._historical_limit = self._read_historical_limit()
        self._historical_candidates_emitted = 0
        self._historical_limit_reached = False
        self._historical_materialization_failed = False
        self._historical_scan_exhausted = False
        self._live_topic_index = 0

    @property
    def mode(self) -> str:
        if self.db is None:
            return "LIVE" if self._historical_complete else "CATCH_UP"
        if self.source_id:
            return self.db.source_sync_mode(self.source_id)
        return self.db.sync_mode()

    @staticmethod
    def _read_historical_limit() -> int | None:
        raw = (os.getenv("ARMORED_SYNC_CATCHUP_LIMIT") or "").strip()
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError:
            print(f"[SYNC][CATCH-UP] Limite inválido {raw!r}; CATCH-UP completo.")
            return None
        if value < 0:
            print(f"[SYNC][CATCH-UP] Limite {value} inválido; use 0 para ilimitado ou um inteiro positivo.")
            return None
        if value == 0:
            print("[SYNC][CATCH-UP] Limite 0 = CATCH-UP ilimitado.")
            return None
        return value

    @property
    def historical_limit_reached(self) -> bool:
        return self._historical_limit_reached

    @property
    def historical_collection_limited(self) -> bool:
        return self._historical_limit is not None

    @property
    def historical_materialization_failed(self) -> bool:
        return self._historical_materialization_failed

    @property
    def historical_scan_exhausted(self) -> bool:
        return self._historical_scan_exhausted

    def complete_historical_sync(self) -> None:
        """Commit final historical checkpoints only after every candidate completed."""
        if self.db is not None and self._historical_checkpoints:
            self.commit_live_checkpoints(self._historical_checkpoints)
        self.mark_historical_complete()
        self._historical_scan_exhausted = True

    def mark_materialization_failed(self) -> None:
        self._historical_materialization_failed = True

    def reset_historical_scan(self) -> None:
        """Rebuild the in-memory historical iterator after durable recovery progress."""
        self._topic_iterator = None
        self._historical_materialization_failed = False
        self._historical_scan_exhausted = False
        self._historical_limit_reached = False
        self._historical_candidates_emitted = 0
        self._historical_checkpoints.clear()
        self._seen.clear()

    def _catchup_limit_before_candidate(self) -> bool:
        if self._historical_limit is None:
            return False
        if self._historical_candidates_emitted >= self._historical_limit:
            self._historical_limit_reached = True
            print(
                f"[SYNC][CATCH-UP] Limite atingido: "
                f"{self._historical_candidates_emitted} candidato(s)."
            )
            return True
        return False

    def mark_historical_complete(self) -> None:
        if self.db is not None:
            if self.source_id:
                self.db.complete_source_historical_sync(self.source_id)
            else:
                self.db.complete_historical_sync()
        self._historical_complete = True
        self._historical_scan_exhausted = True
        self._historical_checkpoints.clear()

    def is_historical_complete(self) -> bool:
        return self.mode == "LIVE"

    @staticmethod
    def _is_video_message(message: Any) -> bool:
        """Accept Telegram videos sent as media or as generic video documents."""
        if message is None:
            return False
        if getattr(message, "video", None):
            return True
        document = getattr(message, "document", None)
        if document is None:
            return False
        mime_type = str(getattr(document, "mime_type", "") or "").lower()
        if mime_type.startswith("video/"):
            return True
        return any(
            "video" in type(attribute).__name__.lower()
            for attribute in (getattr(document, "attributes", None) or ())
        )

    @staticmethod
    def _shopee_url(message: Any) -> str | None:
        if not message:
            return None
        text = str(getattr(message, "message", "") or "")
        for url in TelegramSource.URL_RE.findall(text):
            cleaned = url.rstrip(".,!?;:" + chr(34) + "'()[]{}<>")
            if any(domain in cleaned.lower() for domain in TelegramSource.SHOPEE_DOMAINS):
                return cleaned
        for entity in (getattr(message, "entities", None) or []):
            url = getattr(entity, "url", None)
            if url and any(domain in str(url).lower() for domain in TelegramSource.SHOPEE_DOMAINS):
                return str(url)
        return None

    @staticmethod
    def _topic_id(message: Any) -> int | None:
        topic_id = getattr(message, "reply_to_top_id", None)
        if topic_id is None:
            reply = getattr(message, "reply_to", None)
            topic_id = getattr(reply, "reply_to_top_id", None) if reply else None
        return int(topic_id) if topic_id else None

    def _shopee_url_exists(self, original_url: str | None) -> bool:
        """Return whether a Shopee URL is already safely represented.

        A completed item suppresses rediscovery. An incomplete historical item
        must remain discoverable so Recovery can re-materialize its ORIGINAL.
        WAITING_VISION is also durable and must not be retried automatically.
        """
        if self.db is None or not original_url:
            return False
        conn = getattr(self.db, "conn", None)
        if conn is None:
            return False

        rows = conn.execute(
            "SELECT state, cleanup_completed, original_path, affiliate_url "
            "FROM items WHERE source_id=? "
            "AND LOWER(TRIM(original_url)) = LOWER(TRIM(?))",
            (str(self.source_id or "telegram"), str(original_url)),
        ).fetchall()

        # No durable row means this URL is new for this source.
        if not rows:
            return False

        recoverable = False
        represented = False

        for row in rows:
            state = str(row["state"] or "")
            stored_path = str(row["original_path"] or "").strip()

            # Any safely represented item suppresses rediscovery. This check
            # is intentionally performed across all matching rows before
            # considering an incomplete recovery row, so one failed attempt
            # cannot hide a separately completed publication.
            if state == "PUBLISHED" and bool(row["cleanup_completed"]):
                represented = True
                break

            if stored_path:
                try:
                    if Path(stored_path).is_file():
                        represented = True
                        break
                except OSError:
                    pass

            # These are the only historical states that the Coordinator can
            # intentionally rediscover: a technical recovery or a pre-download
            # item that already contains durable Vision evidence.
            if state == "RECOVERY":
                recoverable = True
                continue
            if state in {"RECEIVED", "VISION"} and str(row["affiliate_url"] or "").strip():
                recoverable = True
                continue

            # WAITING_VISION and every other durable/non-recoverable row still
            # represent a known URL. They must not create a second candidate.
            return True

        if represented:
            return True
        return not recoverable

    async def _discover_topics(self, source: str) -> list[tuple[int, str]]:
        """Discover every forum topic, or fall back to the whole chat history.

        Telegram sources are not required to be forum supergroups. A regular
        channel/group is represented by the synthetic topic 0 ("Geral") and
        streamed through iter_messages. Forum pagination is continued until
        Telegram returns no new topics.
        """
        from telethon import functions

        topics: list[tuple[int, str]] = []
        seen: set[int] = set()
        offset_topic = 0
        offset_id = 0
        offset_date = None
        try:
            result = await self.reader.client(
                functions.messages.GetForumTopicsRequest(
                    peer=source,
                    q=None,
                    offset_date=offset_date,
                    offset_id=offset_id,
                    offset_topic=offset_topic,
                    limit=100,
                )
            )
        except Exception as exc:
            logging.getLogger(__name__).info(
                "[SYNC][DISCOVERY] Fonte sem listagem de tópicos; usando histórico geral (%s)",
                type(exc).__name__,
            )
            return [(0, "Geral")]

        while True:
            page = list(getattr(result, "topics", []) or [])
            new_topics = []
            for topic in page:
                topic_id = getattr(topic, "id", None)
                if topic_id is None:
                    continue
                topic_id = int(topic_id)
                if topic_id in seen:
                    continue
                seen.add(topic_id)
                title = str(getattr(topic, "title", None) or topic_id).strip()
                topics.append((topic_id, title))
                new_topics.append(topic)
            if not page:
                break
            if not new_topics:
                raise RuntimeError(
                    "forum-topic-pagination-stalled; refusing incomplete historical discovery"
                )
            if len(page) < 100:
                break

            last = page[-1]
            next_topic = int(getattr(last, "id", 0) or 0)
            next_id = int(getattr(last, "top_message", 0) or 0)
            next_date = getattr(last, "date", None)
            if not next_topic or next_topic == offset_topic:
                raise RuntimeError(
                    "forum-topic-pagination-offset-stalled; refusing incomplete historical discovery"
                )
            offset_topic, offset_id, offset_date = next_topic, next_id, next_date
            result = await self.reader.client(
                functions.messages.GetForumTopicsRequest(
                    peer=source,
                    q=None,
                    offset_date=offset_date,
                    offset_id=offset_id,
                    offset_topic=offset_topic,
                    limit=100,
                )
            )

        return topics or [(0, "Geral")]

    async def _topic_messages(self, source: str, topic_id: int):
        """Stream history for one forum topic or for an ordinary Telegram chat."""
        if int(topic_id) == 0:
            async for message in self.reader.client.iter_messages(
                source,
                limit=None,
                reverse=True,
            ):
                yield message
            return

        from telethon import functions
        offset_id = 0
        while True:
            result = await self.reader.client(
                functions.messages.GetRepliesRequest(
                    peer=source,
                    msg_id=topic_id,
                    offset_id=offset_id,
                    offset_date=None,
                    add_offset=0,
                    limit=100,
                    max_id=0,
                    min_id=0,
                    hash=0,
                )
            )
            messages = list(getattr(result, "messages", []) or [])
            if not messages:
                return
            for message in messages:
                yield message
            ids = [int(getattr(m, "id", 0) or 0) for m in messages]
            ids = [value for value in ids if value > 0]
            if not ids:
                raise RuntimeError(
                    f"topic-history-page-without-message-ids:{topic_id}"
                )
            oldest = min(ids)
            if offset_id and oldest >= offset_id:
                raise RuntimeError(
                    f"topic-history-pagination-stalled:{topic_id}:offset={offset_id}:oldest={oldest}"
                )
            offset_id = oldest

    async def _download_to(self, message: Any, target: Path) -> None:
        """Materialize one Telegram video without a false total-duration timeout.

        Telegram downloads can legitimately take more than three minutes on a
        slow connection. A wall-clock timeout therefore converts a healthy,
        progressing transfer into a lost candidate. The safety bound here is
        an inactivity timeout per chunk: a transfer may take as long as
        necessary as long as Telegram keeps delivering data.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()

        telegram_size = getattr(getattr(message, "document", None), "size", None)
        idle_timeout = max(
            1,
            int(os.getenv("ARMORED_SYNC_DOWNLOAD_IDLE_TIMEOUT", "60")),
        )
        started = asyncio.get_running_loop().time()
        downloaded = 0
        last_report = 0

        iterator = self.reader.client.iter_download(
            message,
            request_size=1024 * 1024,
        ).__aiter__()

        with target.open("wb") as output:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        iterator.__anext__(),
                        timeout=idle_timeout,
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError as exc:
                    raise TimeoutError(
                        f"download Telegram ficou {idle_timeout}s sem progresso "
                        f"para mensagem {getattr(message, 'id', '?')}"
                    ) from exc

                if not chunk:
                    continue

                output.write(chunk)
                downloaded += len(chunk)
                now = asyncio.get_running_loop().time()
                if now - last_report >= 5:
                    last_report = now
                    if os.getenv("ARMORED_SYNC_VERBOSE_PROGRESS", "0") == "1":
                        if telegram_size:
                            pct = downloaded * 100.0 / int(telegram_size)
                            print(
                                f"[SYNC][DOWNLOAD] {getattr(message, 'id', '?')} "
                                f"{downloaded / 1048576:.1f}/{int(telegram_size) / 1048576:.1f} MiB "
                                f"({pct:.0f}%)"
                            )
                        else:
                            print(
                                f"[SYNC][DOWNLOAD] {getattr(message, 'id', '?')} "
                                f"{downloaded / 1048576:.1f} MiB"
                            )

        actual_size = target.stat().st_size if target.exists() else 0
        elapsed = max(asyncio.get_running_loop().time() - started, 0.001)
        if actual_size <= 0:
            raise RuntimeError("download retornou arquivo vazio")
        if telegram_size is not None and actual_size != int(telegram_size):
            raise RuntimeError(f"download incompleto: {actual_size} bytes de {int(telegram_size)}")
        logging.getLogger(__name__).info(
            "[SYNC][DOWNLOAD] %s OK | %.1f MiB | %.1fs",
            getattr(message, "id", "?"),
            actual_size / 1048576,
            elapsed,
        )

    async def materialize_candidate_async(self, telegram_message_id: str, target: Path) -> None:
        """Re-fetch one already-Vision-approved Telegram video by durable message ID."""
        source = self.source or (os.getenv("ARMORED_SYNC_SOURCE") or "").strip()
        if not source:
            raise RuntimeError("ARMORED_SYNC_SOURCE não configurado")
        source_ref = int(source) if str(source).lstrip("-").isdigit() else source
        await self.reader.connect()
        message = await self.reader.client.get_messages(
            source_ref,
            ids=int(telegram_message_id),
        )
        if isinstance(message, (list, tuple)):
            message = next((value for value in message if value is not None), None)
        if message is None or not self._is_video_message(message):
            raise RuntimeError(
                f"historical-video-not-found:{self.source_id}:{telegram_message_id}"
            )
        await self._download_to(message, target)

    async def fetch_next_async(self) -> SyncMessage | None:
        if self.is_historical_complete():
            return None
        source = self.source or (os.getenv("ARMORED_SYNC_SOURCE") or "").strip()
        if not source:
            raise RuntimeError("ARMORED_SYNC_SOURCE não configurado")
        source_id = self.source_id or (os.getenv("ARMORED_SYNC_SOURCE_ID") or source).strip()
        source_ref = int(source) if str(source).lstrip("-").isdigit() else source

        if not self.reader.client.is_connected():
            await self.reader.connect()

        if self._topic_iterator is None:
            topics = await self._discover_topics(source_ref)
            if not topics:
                raise RuntimeError(f"Nenhum tópico de fórum encontrado na fonte Telegram {source}.")
            self._topic_iterator = self._candidate_iterator(source_ref, topics)

        async for candidate in self._topic_iterator:
            message_id, topic_id, topic_name, message, original_url = candidate
            if message_id in self._seen:
                continue
            # The bounded certification limit belongs to the
            # Coordinator, not to Sync discovery. Sync must behave exactly
            # like production: keep discovering the next eligible candidate
            # until the Coordinator has completed the requested number of
            # full pipeline items.
            self._historical_candidates_emitted += 1
            return SyncMessage(
                telegram_message_id=str(message_id),
                source_id=source_id,
                topic_id=topic_id,
                topic_name=topic_name,
                original_url=original_url,
                materialize=lambda target, m=message: self._download_to(m, target),
            )
        if self._historical_materialization_failed:
            # Do not advance the scan checkpoint to the end of history and do
            # not switch to LIVE. The failed candidate remains recoverable and
            # will be rediscovered after restart.
            return None
        # The Coordinator owns historical checkpoint advancement. Reaching
        # the end of discovery is not sufficient to declare CATCH-UP complete:
        # the last discovered candidates may still be processing.
        self._historical_scan_exhausted = True
        return None

    def _grouped_candidates(
        self,
        messages: list[Any],
        topic_id: int,
        topic_name: str,
    ) -> list[tuple[int, int, str, Any, str | None]]:
        """Resolve video media in an album without silently dropping unlinked videos.

        A single unique Shopee link on an album is attached to one deterministic
        video. With multiple links, only links attached to each video are trusted;
        unlinked videos are still reserved with no URL so Vision can preserve
        them as WAITING_VISION instead of silently skipping Telegram content.
        """
        videos = [message for message in messages if self._is_video_message(message)]
        if not videos:
            return []

        unique_links: dict[str, str] = {}
        for message in messages:
            url = self._shopee_url(message)
            if url:
                unique_links.setdefault(url.casefold(), url)

        if len(unique_links) == 1:
            original_url = next(iter(unique_links.values()))
            if self._shopee_url_exists(original_url):
                return []
            linked_videos = [
                message for message in videos if self._shopee_url(message) is not None
            ]
            selected = min(
                linked_videos or videos,
                key=lambda message: int(getattr(message, "id", 0) or 0),
            )
            selected_id = int(getattr(selected, "id", 0) or 0)
            if selected_id <= 0 or selected_id in self._seen:
                return []
            return [(selected_id, int(topic_id), topic_name, selected, original_url)]

        candidates: list[tuple[int, int, str, Any, str | None]] = []
        emitted_links: set[str] = set()
        for message in sorted(
            videos,
            key=lambda value: int(getattr(value, "id", 0) or 0),
        ):
            message_id = int(getattr(message, "id", 0) or 0)
            if message_id <= 0 or message_id in self._seen:
                continue
            url = self._shopee_url(message)
            if url:
                key = url.casefold()
                if key in emitted_links or self._shopee_url_exists(url):
                    continue
                emitted_links.add(key)
                candidates.append((message_id, int(topic_id), topic_name, message, url))
            elif not unique_links:
                candidates.append((message_id, int(topic_id), topic_name, message, None))
            else:
                # A link attached only to a different media item is ambiguous.
                candidates.append((message_id, int(topic_id), topic_name, message, None))
        return candidates

    async def _candidate_iterator(self, source: str, topics: list[tuple[int, str]]):
        """Stream historical candidates without building a topic-sized list."""
        for topic_id, topic_name in topics:
            pending_video = None
            pending_link: str | None = None
            pending_group_id = None
            pending_group: list[Any] = []
            topic_max_id = 0

            async def flush_group():
                nonlocal pending_group_id, pending_group
                if pending_group:
                    for candidate in self._grouped_candidates(
                        pending_group,
                        int(topic_id),
                        topic_name,
                    ):
                        yield candidate
                pending_group_id = None
                pending_group = []

            async for message in self._topic_messages(source, topic_id):
                message_id = int(getattr(message, "id", 0) or 0)
                if message_id > topic_max_id:
                    topic_max_id = message_id
                if message_id <= 0:
                    continue

                grouped_id = getattr(message, "grouped_id", None)
                if grouped_id is not None:
                    # Album links are resolved within the album itself.
                    pending_link = None
                    grouped_id = int(grouped_id)
                    if pending_group_id is None:
                        if pending_video is not None:
                            pending_id, pending_message = pending_video
                            original_url = self._shopee_url(pending_message)
                            if (
                                pending_id not in self._seen
                                and original_url is not None
                                and not self._shopee_url_exists(original_url)
                            ):
                                yield (
                                    pending_id,
                                    int(topic_id),
                                    topic_name,
                                    pending_message,
                                    original_url,
                                )
                            pending_video = None
                        pending_group_id = grouped_id
                        pending_group = [message]
                    elif grouped_id == pending_group_id:
                        pending_group.append(message)
                    else:
                        async for candidate in flush_group():
                            yield candidate
                        pending_group_id = grouped_id
                        pending_group = [message]
                    continue

                if pending_group:
                    async for candidate in flush_group():
                        yield candidate

                paired_pending_video = False
                if pending_video is not None:
                    pending_id, pending_message = pending_video
                    paired_pending_video = True
                    original_url = (
                        None
                        if self._is_video_message(message)
                        else self._shopee_url(message)
                    )
                    if pending_id not in self._seen:
                        if original_url:
                            if not self._shopee_url_exists(original_url):
                                yield (
                                    pending_id,
                                    int(topic_id),
                                    topic_name,
                                    pending_message,
                                    original_url,
                                )
                        else:
                            # Keep every video in SQLite even without a nearby
                            # product URL; Vision will place it in WAITING_VISION.
                            yield (
                                pending_id,
                                int(topic_id),
                                topic_name,
                                pending_message,
                                None,
                            )
                    pending_video = None

                if not self._is_video_message(message):
                    # In newest-first forum pages, a newer link may precede its
                    # video; in general history the pending_video branch pairs
                    # a link posted after the video.
                    pending_link = (
                        None
                        if paired_pending_video
                        else self._shopee_url(message)
                    )
                    continue

                original_url = self._shopee_url(message)
                if original_url is not None:
                    pending_link = None
                    if (
                        message_id not in self._seen
                        and not self._shopee_url_exists(original_url)
                    ):
                        yield (
                            message_id,
                            int(topic_id),
                            topic_name,
                            message,
                            original_url,
                        )
                    continue

                if pending_link is not None:
                    original_url = pending_link
                    pending_link = None
                    if (
                        message_id not in self._seen
                        and not self._shopee_url_exists(original_url)
                    ):
                        yield (
                            message_id,
                            int(topic_id),
                            topic_name,
                            message,
                            original_url,
                        )
                    continue

                pending_video = (message_id, message)

            if pending_group:
                async for candidate in flush_group():
                    yield candidate

            if pending_video is not None:
                pending_id, pending_message = pending_video
                if pending_id not in self._seen:
                    yield (
                        pending_id,
                        int(topic_id),
                        topic_name,
                        pending_message,
                        None,
                    )

            if topic_max_id and int(topic_id) > 0:
                self._historical_checkpoints[topic_id] = topic_max_id

    async def iter_historical_candidates_async(self):
        """Yield historical candidates using the canonical discovery logic."""
        if self.is_historical_complete():
            return

        source = self.source or (os.getenv("ARMORED_SYNC_SOURCE") or "").strip()
        if not source:
            raise RuntimeError("ARMORED_SYNC_SOURCE não configurado")
        source_id = self.source_id or (os.getenv("ARMORED_SYNC_SOURCE_ID") or source).strip()
        source_ref = int(source) if str(source).lstrip("-").isdigit() else source

        await self.reader.connect()
        if self._historical_limit is not None:
            print(f"[SYNC][CATCH-UP] Limite de candidatos: {self._historical_limit}")
        topics = await self._discover_topics(source_ref)
        if not topics:
            raise RuntimeError(f"Nenhum tópico de fórum encontrado na fonte Telegram {source}.")

        self._historical_checkpoints.clear()
        self._historical_candidates_emitted = 0
        self._historical_limit_reached = False
        self._topic_iterator = self._candidate_iterator(source_ref, topics)
        try:
            async for candidate in self._topic_iterator:
                message_id, topic_id, topic_name, message, original_url = candidate
                if message_id in self._seen:
                    continue
                if self._catchup_limit_before_candidate():
                    return
                self._historical_candidates_emitted += 1
                yield SyncMessage(
                    telegram_message_id=str(message_id),
                    source_id=source_id,
                    topic_id=topic_id,
                    topic_name=topic_name,
                    original_url=original_url,
                    materialize=lambda target, m=message: self._download_to(m, target),
                )
            self._historical_scan_exhausted = True
        except Exception:
            await self.reader.disconnect()
            raise

    async def collect_historical_batch_async(self) -> tuple[list[SyncMessage], dict[int, int]]:
        """Collect historical candidates using the same grouped discovery rules."""
        if self.is_historical_complete():
            return [], {}

        source = self.source or (os.getenv("ARMORED_SYNC_SOURCE") or "").strip()
        if not source:
            raise RuntimeError("ARMORED_SYNC_SOURCE não configurado")
        source_id = self.source_id or (os.getenv("ARMORED_SYNC_SOURCE_ID") or source).strip()
        source_ref = int(source) if str(source).lstrip("-").isdigit() else source

        await self.reader.connect()
        try:
            topics = await self._discover_topics(source_ref)
            if not topics:
                raise RuntimeError(f"Nenhum tópico de fórum encontrado na fonte Telegram {source}.")

            self._historical_checkpoints.clear()
            candidates: list[SyncMessage] = []
            async for message_id, topic_id, topic_name, message, original_url in self._candidate_iterator(
                source_ref,
                topics,
            ):
                if message_id in self._seen:
                    continue
                candidates.append(SyncMessage(
                    telegram_message_id=str(message_id),
                    source_id=source_id,
                    topic_id=topic_id,
                    topic_name=topic_name,
                    original_url=original_url,
                    materialize=lambda target, m=message: self._download_to(m, target),
                ))
            self._historical_scan_exhausted = True
            return candidates, dict(self._historical_checkpoints)
        except Exception:
            await self.reader.disconnect()
            raise

    async def fetch_live_batch_async(self, limit: int | None = None) -> tuple[list[SyncMessage], dict[int, int]]:
        """Discover all new LIVE candidates, resolving grouped albums consistently."""
        source = self.source or (os.getenv("ARMORED_SYNC_SOURCE") or "").strip()
        if not source:
            raise RuntimeError("ARMORED_SYNC_SOURCE não configurado")
        source_id = self.source_id or (os.getenv("ARMORED_SYNC_SOURCE_ID") or source).strip()
        source_ref = int(source) if str(source).lstrip("-").isdigit() else source

        await self.reader.connect()
        try:
            if self._topics is None:
                self._topics = await self._discover_topics(source_ref)
            if not self._topics:
                raise RuntimeError(f"Nenhum tópico de fórum encontrado na fonte Telegram {source}.")

            candidates: list[SyncMessage] = []
            checkpoints: dict[int, int] = {}
            for topic_id, topic_name in self._topics:
                checkpoint = (
                    self.db.source_sync_topic_checkpoint(self.source_id, topic_id)
                    if self.db is not None and self.source_id
                    else (self.db.sync_topic_checkpoint(topic_id) if self.db is not None else 0)
                )
                messages = []
                async for message in self.reader.client.iter_messages(
                    source_ref,
                    reply_to=topic_id,
                    min_id=max(0, checkpoint),
                    reverse=True,
                ):
                    messages.append(message)

                ids = [int(getattr(message, "id", 0) or 0) for message in messages]
                if ids:
                    checkpoints[topic_id] = max(checkpoint, max(ids))

                index = 0
                while index < len(messages):
                    message = messages[index]
                    message_id = int(getattr(message, "id", 0) or 0)
                    if message_id <= checkpoint or message_id in self._seen:
                        index += 1
                        continue

                    grouped_id = getattr(message, "grouped_id", None)
                    if grouped_id is not None:
                        grouped_id = int(grouped_id)
                        group = [message]
                        next_index = index + 1
                        while next_index < len(messages):
                            next_message = messages[next_index]
                            next_grouped_id = getattr(next_message, "grouped_id", None)
                            if next_grouped_id is None or int(next_grouped_id) != grouped_id:
                                break
                            group.append(next_message)
                            next_index += 1

                        for candidate_id, _, _, candidate_message, original_url in self._grouped_candidates(
                            group,
                            int(topic_id),
                            topic_name,
                        ):
                            if candidate_id > checkpoint and candidate_id not in self._seen:
                                candidates.append(SyncMessage(
                                    telegram_message_id=str(candidate_id),
                                    source_id=source_id,
                                    topic_id=topic_id,
                                    topic_name=topic_name,
                                    original_url=original_url,
                                    materialize=lambda target, m=candidate_message: self._download_to(m, target),
                                ))
                                if limit is not None and len(candidates) >= limit:
                                    return candidates, checkpoints
                        index = next_index
                        continue

                    if self._is_video_message(message):
                        original_url = self._shopee_url(message)
                        if original_url is None and index + 1 < len(messages):
                            next_message = messages[index + 1]
                            if not self._is_video_message(next_message):
                                original_url = self._shopee_url(next_message)
                        if (
                            original_url is not None
                            and not self._shopee_url_exists(original_url)
                        ):
                            candidates.append(SyncMessage(
                                telegram_message_id=str(message_id),
                                source_id=source_id,
                                topic_id=topic_id,
                                topic_name=topic_name,
                                original_url=original_url,
                                materialize=lambda target, m=message: self._download_to(m, target),
                            ))
                            if limit is not None and len(candidates) >= limit:
                                return candidates, checkpoints
                    index += 1

            return candidates, checkpoints
        except Exception:
            await self.reader.disconnect()
            raise

    async def fetch_live_candidate_async(self) -> tuple[SyncMessage | None, dict[int, int]]:
        """Discover one LIVE candidate using a round-robin topic poll.

        A new media group may contain several photos/videos and place its
        Shopee URL on a different media item. Inspect a bounded window of new
        messages so one grouped album can be resolved as one content without
        sweeping the entire topic.
        """
        source = self.source or (os.getenv("ARMORED_SYNC_SOURCE") or "").strip()
        if not source:
            raise RuntimeError("ARMORED_SYNC_SOURCE não configurado")
        source_id = self.source_id or (os.getenv("ARMORED_SYNC_SOURCE_ID") or source).strip()
        source_ref = int(source) if str(source).lstrip("-").isdigit() else source

        await self.reader.connect()
        try:
            if self._topics is None:
                self._topics = await self._discover_topics(source_ref)
            if not self._topics:
                raise RuntimeError(
                    f"Nenhum tópico de fórum encontrado na fonte Telegram {source}."
                )

            topic_id, topic_name = self._topics[self._live_topic_index % len(self._topics)]
            self._live_topic_index = (self._live_topic_index + 1) % len(self._topics)

            checkpoint = (
                self.db.source_sync_topic_checkpoint(self.source_id, topic_id)
                if self.db is not None and self.source_id
                else (self.db.sync_topic_checkpoint(topic_id) if self.db is not None else 0)
            )

            try:
                scan_limit = max(
                    3,
                    int(os.getenv("ARMORED_LIVE_GROUP_SCAN_LIMIT", "20")),
                )
            except ValueError:
                scan_limit = 20

            messages = []
            async for message in self.reader.client.iter_messages(
                source_ref,
                reply_to=topic_id,
                min_id=max(0, checkpoint),
                reverse=True,
                limit=scan_limit,
            ):
                messages.append(message)

            safe_checkpoint = checkpoint
            index = 0
            while index < len(messages):
                message = messages[index]
                message_id = int(getattr(message, "id", 0) or 0)
                if message_id <= checkpoint:
                    index += 1
                    continue
                if message_id in self._seen:
                    safe_checkpoint = max(safe_checkpoint, message_id)
                    index += 1
                    continue

                grouped_id = getattr(message, "grouped_id", None)
                if grouped_id is not None:
                    grouped_id = int(grouped_id)
                    group = [message]
                    next_index = index + 1
                    while next_index < len(messages):
                        next_message = messages[next_index]
                        next_grouped_id = getattr(next_message, "grouped_id", None)
                        if next_grouped_id is None or int(next_grouped_id) != grouped_id:
                            break
                        group.append(next_message)
                        next_index += 1

                    candidates = self._grouped_candidates(
                        group,
                        int(topic_id),
                        topic_name,
                    )
                    for candidate_id, _, _, candidate_message, original_url in candidates:
                        if candidate_id <= checkpoint or candidate_id in self._seen:
                            continue
                        group_max_id = max(
                            int(getattr(item, "id", 0) or 0)
                            for item in group
                        )
                        candidate_checkpoint = (
                            group_max_id if len(candidates) == 1 else candidate_id
                        )
                        return (
                            SyncMessage(
                                telegram_message_id=str(candidate_id),
                                source_id=source_id,
                                topic_id=topic_id,
                                topic_name=topic_name,
                                original_url=original_url,
                                materialize=lambda target, m=candidate_message:
                                    self._download_to(m, target),
                            ),
                            {topic_id: candidate_checkpoint},
                        )

                    safe_checkpoint = max(
                        safe_checkpoint,
                        max(int(getattr(item, "id", 0) or 0) for item in group),
                    )
                    index = next_index
                    continue

                if not self._is_video_message(message):
                    safe_checkpoint = max(safe_checkpoint, message_id)
                    index += 1
                    continue

                original_url = self._shopee_url(message)
                if original_url is None and index + 1 < len(messages):
                    next_message = messages[index + 1]
                    if not getattr(next_message, "video", None):
                        original_url = self._shopee_url(next_message)

                if original_url is None or self._shopee_url_exists(original_url):
                    safe_checkpoint = max(safe_checkpoint, message_id)
                    index += 1
                    continue

                return (
                    SyncMessage(
                        telegram_message_id=str(message_id),
                        source_id=source_id,
                        topic_id=topic_id,
                        topic_name=topic_name,
                        original_url=original_url,
                        materialize=lambda target, m=message:
                            self._download_to(m, target),
                    ),
                    {topic_id: message_id},
                )

            return (
                None,
                {topic_id: safe_checkpoint} if safe_checkpoint > checkpoint else {},
            )
        except Exception:
            await self.reader.disconnect()
            raise

    async def prepare_live_cutover_async(self) -> dict[int, int]:
        """Close a bounded certification window and enter LIVE safely.

        This method is only used when ARMORED_CERT_CATCHUP_THEN_LIVE=1.
        It advances each discovered topic checkpoint to its current newest
        Telegram message ID, then marks historical sync complete. This prevents
        old backlog from being reinterpreted as LIVE after a certification
        cutoff. Normal production CATCH-UP behavior is unchanged.
        """
        source = self.source or (os.getenv("ARMORED_SYNC_SOURCE") or "").strip()
        if not source:
            raise RuntimeError("ARMORED_SYNC_SOURCE não configurado")
        source_ref = int(source) if str(source).lstrip("-").isdigit() else source

        await self.reader.connect()
        try:
            if self._topics is None:
                self._topics = await self._discover_topics(source_ref)
            if not self._topics:
                raise RuntimeError(
                    f"Nenhum tópico de fórum encontrado na fonte Telegram {source}."
                )

            checkpoints: dict[int, int] = {}
            for topic_id, _topic_name in self._topics:
                # _topic_messages() is newest-first. Read only the first
                # available message so the cutover does not scan the backlog.
                async for message in self._topic_messages(source_ref, topic_id):
                    message_id = int(getattr(message, "id", 0) or 0)
                    if message_id > 0:
                        checkpoints[int(topic_id)] = message_id
                    break

            if checkpoints:
                self.commit_live_checkpoints(checkpoints)

            self.mark_historical_complete()
            return checkpoints
        except Exception:
            await self.reader.disconnect()
            raise

    def commit_live_checkpoints(self, checkpoints: dict[int, int]) -> None:
        if self.db is None:
            return
        for topic_id, message_id in checkpoints.items():
            topic_name = next(
                (name for tid, name in (self._topics or []) if tid == topic_id),
                str(topic_id),
            )
            if self.source_id:
                self.db.set_source_sync_topic_checkpoint(
                    self.source_id, topic_id, topic_name, message_id
                )
            else:
                self.db.set_sync_topic_checkpoint(topic_id, topic_name, message_id)

    def fetch_live_batch(self) -> tuple[list[SyncMessage], dict[int, int]]:
        return asyncio.run(self.fetch_live_batch_async())

    def mark_ingested(self, telegram_message_id: str) -> None:
        self._seen.add(int(telegram_message_id))

    def fetch_next(self) -> SyncMessage | None:
        return asyncio.run(self.fetch_next_async())


class MultiTelegramSource:
    """Sequential adapter over multiple Telegram sources with one active candidate."""

    def __init__(self, root: Path, reader: Any, db: Database, routes):
        self.root = Path(root)
        self.reader = reader
        self.db = db
        self.routes = tuple(routes)
        self.sources = tuple(
            TelegramSource(
                root,
                reader,
                db,
                source=route.source.chat_id,
                source_id=route.source.source_id,
            )
            for route in self.routes
        )
        self._cursor = 0
        self._last_source: TelegramSource | None = None
        self._pending_source: TelegramSource | None = None
        self._pending_message: SyncMessage | None = None
        self._pending_checkpoints: dict[int, int] = {}

    @property
    def mode(self) -> str:
        ids = [source.source_id for source in self.sources if source.source_id]
        return "LIVE" if self.db.all_sources_historical_complete(ids) else "CATCH_UP"

    @property
    def historical_materialization_failed(self) -> bool:
        return any(source.historical_materialization_failed for source in self.sources)

    @property
    def historical_scan_exhausted(self) -> bool:
        # Sources already marked LIVE in SQLite need no iterator exhaustion in
        # this process; unfinished sources must have completed their scan.
        return bool(self.sources) and all(
            source.is_historical_complete() or source.historical_scan_exhausted
            for source in self.sources
        )

    @property
    def historical_limit_reached(self) -> bool:
        return any(source.historical_limit_reached for source in self.sources)

    @property
    def historical_collection_limited(self) -> bool:
        return any(source.historical_collection_limited for source in self.sources)

    def is_historical_complete(self) -> bool:
        return all(source.is_historical_complete() for source in self.sources)

    async def iter_historical_candidates_async(self):
        """Stream all sources' historical candidates without downloading media.

        Candidates are reserved and Vision-gated by the Coordinator as they
        stream. Each source's topic checkpoints remain pending until the
        Coordinator confirms the complete staged catch-up.
        """
        for source in self.sources:
            if source.is_historical_complete():
                continue
            async for message in source.iter_historical_candidates_async():
                self._last_source = source
                yield message
            # ARMORED_SYNC_CATCHUP_LIMIT is a global certification boundary:
            # do not start the next source after one source reaches that limit.
            if source.historical_limit_reached:
                return

    async def materialize_candidate_async(
        self, source_id: str, telegram_message_id: str, target: Path
    ) -> None:
        source = next(
            (candidate for candidate in self.sources
             if str(candidate.source_id) == str(source_id)),
            None,
        )
        if source is None:
            raise RuntimeError(f"unknown-source-for-materialization:{source_id}")
        self._last_source = source
        await source.materialize_candidate_async(telegram_message_id, target)

    def commit_candidate_checkpoint(
        self, source_id: str, checkpoints: dict[int, int]
    ) -> None:
        source = next(
            (candidate for candidate in self.sources
             if str(candidate.source_id) == str(source_id)),
            None,
        )
        if source is None:
            raise RuntimeError(f"unknown-source-for-checkpoint:{source_id}")
        source.commit_live_checkpoints(checkpoints)

    async def fetch_next_async(self) -> SyncMessage | None:
        """Return the next historical candidate from the first unfinished source.

        Historical CATCH-UP is intentionally sequential across sources:
        Source 1 must reach durable historical_complete before Source 2 is
        allowed to discover or process its first candidate. A source that is
        blocked by RECOVERY/materialization failure also blocks every later
        source. Round-robin is reserved for LIVE polling only.
        """
        if not self.sources:
            raise RuntimeError("Nenhuma fonte Telegram configurada")

        for index, source in enumerate(self.sources):
            if source.is_historical_complete():
                continue

            message = await source.fetch_next_async()
            if message is not None:
                self._last_source = source
                return message

            if source.historical_materialization_failed:
                # The current source owns the blocked checkpoint. Do not leak
                # discovery into a later source while it is unresolved.
                return None

            if source.historical_scan_exhausted:
                source.complete_historical_sync()
                import logging
                logging.getLogger(__name__).info(
                    "[SYNC][CATCH-UP] Fonte %s historical_complete; "
                    "liberando próxima fonte",
                    source.source_id,
                )
                # Continue in this same call only after the source itself has
                # durably transitioned to LIVE/historical_complete.
                continue

            # No candidate and no completion: keep this source authoritative.
            return None

        if self.sources and all(source.is_historical_complete() for source in self.sources):
            self.db.complete_historical_sync()
        return None

    def complete_historical_sync(self) -> None:
        for source in self.sources:
            if (
                source.historical_scan_exhausted
                and not source.historical_materialization_failed
                and not source.is_historical_complete()
            ):
                source.complete_historical_sync()
        if self.sources and all(source.is_historical_complete() for source in self.sources):
            self.db.complete_historical_sync()

    def mark_ingested(self, telegram_message_id: str) -> None:
        if self._last_source is not None:
            self._last_source.mark_ingested(telegram_message_id)

    def mark_materialization_failed(self) -> None:
        if self._last_source is not None:
            self._last_source.mark_materialization_failed()

    def reset_historical_scan(self) -> None:
        for source in self.sources:
            source.reset_historical_scan()
        self._pending_source = None
        self._pending_message = None
        self._pending_checkpoints = {}

    async def fetch_live_candidate_async(self) -> tuple[SyncMessage | None, dict[int, int]]:
        if self._pending_message is not None:
            return self._pending_message, dict(self._pending_checkpoints)
        if not self.sources:
            return None, {}
        for offset in range(len(self.sources)):
            index = (self._cursor + offset) % len(self.sources)
            source = self.sources[index]
            message, checkpoints = await source.fetch_live_candidate_async()
            self._cursor = (index + 1) % len(self.sources)
            if message is None:
                if checkpoints:
                    source.commit_live_checkpoints(checkpoints)
                continue
            self._last_source = source
            self._pending_source = source
            self._pending_message = message
            self._pending_checkpoints = dict(checkpoints)
            return message, dict(checkpoints)
        return None, {}

    async def fetch_live_batch_async(self, limit: int | None = None):
        message, checkpoints = await self.fetch_live_candidate_async()
        if message is None:
            return [], checkpoints
        return [message], checkpoints

    def commit_live_checkpoints(self, checkpoints: dict[int, int]) -> None:
        # CATCH-UP commits belong to the source that produced the current
        # candidate; LIVE commits belong to the pending source. In both cases
        # only one source checkpoint is advanced for the active item.
        target = self._pending_source or self._last_source
        if target is not None:
            target.commit_live_checkpoints(self._pending_checkpoints or checkpoints)
        self._pending_source = None
        self._pending_message = None
        self._pending_checkpoints = {}

    def connect(self):
        return self.reader.connect()

    def disconnect(self):
        return self.reader.disconnect()


class ArmoredSync:
    """Source + canonical ingestion boundary."""

    def __init__(self, sync: SyncService, source: Any):
        self.sync = sync
        self.source = source

    def fetch_next(self) -> SyncMessage | None:
        value = self.source.fetch_next()
        if hasattr(value, "__await__"):
            value = asyncio.run(value)
        return value

    def ingest(self, message: SyncMessage) -> int:
        return self.sync.ingest_message(
            IngestMessage(
                telegram_message_id=message.telegram_message_id,
                source_id=message.source_id,
                topic_id=message.topic_id,
                topic_name=message.topic_name,
                original_url=message.original_url,
                source_path=message.source_path,
                materialize=message.materialize,
            )
        )
