from __future__ import annotations

import asyncio
import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from armored_core.database import Database
from armored_core.models import Item, PublicationCheck
from armored_core.services import PublicationResult, PublicationUnknownError


class ArmoredHub:
    """Telegram publication boundary; SQLite is the only durable state."""

    def __init__(self, root: Path, db: Database | None = None):
        self.root = Path(root)
        self.db = db
        self._destination_chat_id: str | None = None

    def _publication(self, item: Item):
        return self.db.publication(item.content_id) if self.db is not None else None

    @staticmethod
    def _run_async(coro):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="armored-hub-telegram") as executor:
            return executor.submit(asyncio.run, coro).result()

    def check_publication(self, item: Item) -> PublicationCheck:
        record = self._publication(item)
        if record is None:
            return PublicationCheck.UNKNOWN

        if record["confirmed"]:
            message_id = record["published_message_id"]
            if not message_id:
                return PublicationCheck.UNKNOWN
            status = self._verify_telegram_message(str(message_id), item)
            if status is True:
                return PublicationCheck.CONFIRMED
            if status is None:
                return PublicationCheck.UNKNOWN
            return PublicationCheck.ABSENT

        message_id = record["published_message_id"]
        if message_id:
            status = self._verify_telegram_message(str(message_id), item)
            if status is True:
                self.db.publication_confirmed(item.content_id, str(message_id))
                return PublicationCheck.CONFIRMED
            if status is None:
                return PublicationCheck.UNKNOWN
            return PublicationCheck.ABSENT

        attempts = max(1, int(os.getenv("ARMORED_TELEGRAM_VERIFY_ATTEMPTS", "3")))
        delay = max(0.0, float(os.getenv("ARMORED_TELEGRAM_VERIFY_RETRY_DELAY", "2")))
        send_started = str(record["verification_status"] or "") == "SENT_UNVERIFIED"

        for attempt in range(1, attempts + 1):
            matches = self._find_telegram_publications(item)
            if matches is not None:
                if len(matches) == 1:
                    self.db.publication_confirmed(item.content_id, matches[0])
                    return PublicationCheck.CONFIRMED
                if len(matches) > 1:
                    print(
                        f"[HUB][VERIFY][UNKNOWN] item={item.content_id} "
                        f"multiple exact matches={len(matches)}"
                    )
                    return PublicationCheck.UNKNOWN

                if attempt < attempts and delay:
                    time.sleep(delay)
                    continue
                return PublicationCheck.UNKNOWN if send_started else PublicationCheck.ABSENT

            print(
                f"[HUB][VERIFY] item={item.content_id} "
                f"attempt={attempt}/{attempts} returned UNKNOWN"
            )
            if attempt < attempts and delay:
                time.sleep(delay)

        return PublicationCheck.UNKNOWN

    def publish_once(self, item: Item) -> PublicationResult:
        if self.db is None:
            raise RuntimeError("ArmoredHub exige Database para publicação idempotente")

        existing = self._publication(item)
        self.db.publication_started(item.item_id)
        check = PublicationCheck.ABSENT if existing is None else self.check_publication(item)

        if check == PublicationCheck.CONFIRMED:
            record = self._publication(item)
            message_id = record["published_message_id"] if record else None
            if not message_id:
                raise PublicationUnknownError("publication-confirmed-without-real-message-id")
            return PublicationResult(True, str(message_id))

        if check == PublicationCheck.UNKNOWN:
            raise PublicationUnknownError("Telegram publication outcome is UNKNOWN")

        return self.publish(item)

    def _telegram_session_path(self) -> Path:
        return self.root / "credentials" / "telegram" / "session" / "armoredsync"

    def _resolve_destination_chat_id(self, topic_id: str | int | None = None) -> str | None:
        configured = (os.getenv("ARMORED_CREATOR_GROUP_ID") or "").strip()
        if configured:
            self._destination_chat_id = configured
            return configured

        if self._destination_chat_id:
            return self._destination_chat_id

        topic_value = str(topic_id or (os.getenv("ARMORED_HUB_TOPIC_ID") or "228")).strip()
        if not topic_value or not topic_value.lstrip("-").isdigit():
            raise RuntimeError("ARMORED_HUB_TOPIC_ID inválido para descoberta automática")

        api_id = os.getenv("TELEGRAM_API_ID")
        api_hash = os.getenv("TELEGRAM_API_HASH")
        if not api_id or not api_hash:
            raise RuntimeError(
                "Descoberta automática do grupo exige TELEGRAM_API_ID e TELEGRAM_API_HASH"
            )

        async def discover() -> str | None:
            try:
                from telethon import TelegramClient, functions
            except ImportError as exc:
                raise RuntimeError("Dependência Telethon ausente para descoberta do Hub") from exc

            session = self._telegram_session_path()
            session.parent.mkdir(parents=True, exist_ok=True)
            client = TelegramClient(
                str(session),
                int(api_id),
                api_hash,
                request_retries=0,
                connection_retries=0,
            )
            matches: list[str] = []
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    raise RuntimeError(
                        "Sessão Telegram do ArmoredSync não está autenticada; "
                        "o Hub não fará login interativo."
                    )
                async for dialog in client.iter_dialogs():
                    entity = getattr(dialog, "entity", None)
                    if entity is None:
                        continue
                    if not bool(getattr(entity, "megagroup", False) or getattr(entity, "forum", False)):
                        continue
                    try:
                        result = await client(
                            functions.messages.GetForumTopicsRequest(
                                peer=entity,
                                q=None,
                                offset_date=None,
                                offset_id=0,
                                offset_topic=0,
                                limit=100,
                            )
                        )
                    except Exception:
                        continue
                    for topic in getattr(result, "topics", []) or []:
                        if str(getattr(topic, "id", "")) == topic_value:
                            matches.append(str(dialog.id))
                            break

                unique_matches = sorted(set(matches))
                if len(unique_matches) == 1:
                    self._destination_chat_id = unique_matches[0]
                    return unique_matches[0]
                if not unique_matches:
                    raise RuntimeError(
                        f"Nenhum grupo-fórum com tópico {topic_value} foi encontrado "
                        "pela sessão Telegram existente"
                    )
                raise RuntimeError(
                    f"Tópico {topic_value} encontrado em múltiplos grupos: "
                    + ", ".join(unique_matches)
                )
            finally:
                if client.is_connected():
                    await client.disconnect()

        return self._run_async(discover())

    @staticmethod
    def _topic_id(message) -> int | None:
        reply_to = getattr(message, "reply_to", None)
        if reply_to is None:
            return None
        value = getattr(reply_to, "reply_to_top_id", None)
        if value is None:
            value = getattr(reply_to, "reply_to_msg_id", None)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _publication_text(item: Item) -> str:
        links = list(item.affiliate_urls or ())
        if not links and item.affiliate_url:
            links = [str(item.affiliate_url).strip()]
        links = list(dict.fromkeys(str(link).strip() for link in links if str(link).strip()))[:7]
        caption = str(item.publication_caption or "").strip()
        if caption and links:
            text = caption + "\n\n" + "\n".join(links)
        elif caption:
            text = caption
        else:
            text = "\n".join(links)
        if len(text) > 1024:
            raise RuntimeError("Hub caption/publication package excede 1024 caracteres")
        return text

    def _telegram_publication_matches(self, message, item: Item, topic_id: int, topic_scoped: bool = False) -> bool:
        """Require exact caption/media; topic scope may already be guaranteed by the query."""
        if not topic_scoped and self._topic_id(message) != int(topic_id):
            return False

        caption = str(getattr(message, "message", "") or "").strip()
        if caption != self._publication_text(item):
            return False

        if not getattr(message, "video", None) and not getattr(message, "document", None):
            return False

        return True

    def _find_telegram_publications(self, item: Item) -> list[str] | None:
        api_id = os.getenv("TELEGRAM_API_ID")
        api_hash = os.getenv("TELEGRAM_API_HASH")
        topic_id = (os.getenv("ARMORED_HUB_TOPIC_ID") or "228").strip()
        if not api_id or not api_hash or not topic_id:
            return None

        try:
            chat_id = self._resolve_destination_chat_id(topic_id)
        except Exception as exc:
            print(
                f"[HUB][VERIFY][UNKNOWN] item={item.content_id} "
                f"destination-resolution: {type(exc).__name__}: {exc}"
            )
            return None
        if not chat_id:
            return None

        affiliate = str(item.affiliate_url or "").strip()
        query = affiliate.rstrip("/").rsplit("/", 1)[-1].strip() if affiliate else ""

        async def find():
            try:
                from telethon import TelegramClient, functions
                from telethon.tl.types import InputMessagesFilterEmpty
            except ImportError:
                return None

            session = self._telegram_session_path()
            session.parent.mkdir(parents=True, exist_ok=True)
            client = TelegramClient(
                str(session),
                int(api_id),
                api_hash,
                request_retries=0,
                connection_retries=0,
            )
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    return None

                entity = await client.get_entity(int(chat_id))
                exact_matches: list[str] = []

                async for message in client.iter_messages(
                    entity,
                    limit=int(os.getenv("ARMORED_TELEGRAM_VERIFY_HISTORY_LIMIT", "200")),
                    reply_to=int(topic_id),
                ):
                    if self._telegram_publication_matches(message, item, int(topic_id), topic_scoped=True):
                        message_id = str(getattr(message, "id", ""))
                        if message_id:
                            exact_matches.append(message_id)

                if exact_matches:
                    return sorted(set(exact_matches))

                if not query:
                    return []

                result = await client(
                    functions.messages.SearchRequest(
                        peer=entity,
                        q=query,
                        from_id=None,
                        top_msg_id=int(topic_id),
                        filter=InputMessagesFilterEmpty(),
                        min_date=None,
                        max_date=None,
                        offset_id=0,
                        add_offset=0,
                        limit=100,
                        max_id=0,
                        min_id=0,
                        hash=0,
                    )
                )
                for message in getattr(result, "messages", []) or []:
                    if self._telegram_publication_matches(message, item, int(topic_id), topic_scoped=True):
                        message_id = str(getattr(message, "id", ""))
                        if message_id:
                            exact_matches.append(message_id)

                return sorted(set(exact_matches))
            finally:
                if client.is_connected():
                    await client.disconnect()

        try:
            result = self._run_async(find())
            count = len(result) if result is not None else "UNKNOWN"
            print(
                f"[HUB][VERIFY] item={item.content_id} query={query!r} "
                f"topic={topic_id} chat={chat_id} matches={count}"
            )
            return result
        except Exception as exc:
            print(
                f"[HUB][VERIFY][UNKNOWN] item={item.content_id} query={query!r}: "
                f"{type(exc).__name__}: {exc}"
            )
            return None

    def _verify_telegram_message(self, message_id: str, item: Item) -> bool | None:
        api_id = os.getenv("TELEGRAM_API_ID")
        api_hash = os.getenv("TELEGRAM_API_HASH")
        topic_id = (os.getenv("ARMORED_HUB_TOPIC_ID") or "228").strip()
        if not message_id or not api_id or not api_hash or not topic_id:
            return None
        try:
            chat_id = self._resolve_destination_chat_id(topic_id)
        except Exception:
            return None
        if not chat_id:
            return None

        async def verify():
            try:
                from telethon import TelegramClient
            except ImportError:
                return None
            session = self._telegram_session_path()
            session.parent.mkdir(parents=True, exist_ok=True)
            client = TelegramClient(str(session), int(api_id), api_hash, request_retries=0, connection_retries=0)
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    return None
                message = await client.get_messages(int(chat_id), ids=int(message_id))
                if message is None:
                    return False
                return self._telegram_publication_matches(message, item, int(topic_id), topic_scoped=True)
            finally:
                if client.is_connected():
                    await client.disconnect()

        try:
            result = self._run_async(verify())
            print(f"[HUB][VERIFY] item={item.content_id} message_id={message_id} topic={topic_id} chat={chat_id} result={result}")
            return result
        except Exception as exc:
            print(f"[HUB][VERIFY][UNKNOWN] item={item.content_id} message_id={message_id}: {type(exc).__name__}: {exc}")
            return None

    def publish(self, item: Item) -> PublicationResult:
        if self.db is None:
            raise RuntimeError("ArmoredHub exige Database para publicação idempotente")

        existing = self.db.publication(item.item_id)
        if existing and existing["confirmed"]:
            return PublicationResult(True, existing["published_message_id"])

        if existing is None:
            self.db.publication_started(item.item_id)

        output = Path(item.result_path or "")
        if not output.is_file() or output.stat().st_size <= 0:
            raise RuntimeError("Hub recebeu resultado inexistente/vazio")

        if os.getenv("ARMORED_HUB_DRY_RUN", "0") == "1":
            raise RuntimeError(
                "ARMORED_HUB_DRY_RUN=1: publicação real bloqueada; "
                "nenhum item pode ser marcado como PUBLISHED"
            )

        return self._publish_telegram(item, output)

    @staticmethod
    def _video_metadata(output: Path) -> tuple[int, int, int]:
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError("Dependência OpenCV ausente para metadados do vídeo") from exc

        capture = cv2.VideoCapture(str(output))
        try:
            if not capture.isOpened():
                raise RuntimeError("Não foi possível abrir o vídeo para leitura de metadados")
            width = int(round(capture.get(cv2.CAP_PROP_FRAME_WIDTH)))
            height = int(round(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)))
            frame_count = float(capture.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = float(capture.get(cv2.CAP_PROP_FPS))
        finally:
            capture.release()

        if width <= 0 or height <= 0:
            raise RuntimeError("Vídeo possui dimensões inválidas")
        if frame_count <= 0 or fps <= 0:
            raise RuntimeError("Vídeo possui duração inválida")

        duration = max(1, int(round(frame_count / fps)))
        return width, height, duration

    def _publish_telegram(self, item: Item, output: Path) -> PublicationResult:
        token = os.getenv("ARMORED_CREATOR_BOT_TOKEN")
        topic_id = (os.getenv("ARMORED_HUB_TOPIC_ID") or "228").strip()
        if not token or not topic_id:
            raise RuntimeError(
                "Telegram Hub exige ARMORED_CREATOR_BOT_TOKEN e ARMORED_HUB_TOPIC_ID"
            )
        chat_id = self._resolve_destination_chat_id(topic_id)
        width, height, duration = self._video_metadata(output)
        self.db.publication_started(
            item.item_id,
            destination_chat_id=chat_id,
            destination_topic_id=int(topic_id),
        )

        try:
            from telegram import Bot
            from telegram.error import NetworkError, TimedOut
            from telegram.request import HTTPXRequest
        except ImportError as exc:
            raise RuntimeError(
                "Dependência python-telegram-bot ausente; instale as dependências do Hub."
            ) from exc

        async def send():
            request = HTTPXRequest(
                connection_pool_size=int(os.getenv("ARMORED_TELEGRAM_CONNECTION_POOL_SIZE", "4")),
                connect_timeout=float(os.getenv("ARMORED_TELEGRAM_CONNECT_TIMEOUT", "15")),
                read_timeout=float(os.getenv("ARMORED_TELEGRAM_READ_TIMEOUT", "60")),
                write_timeout=float(os.getenv("ARMORED_TELEGRAM_WRITE_TIMEOUT", "180")),
                pool_timeout=float(os.getenv("ARMORED_TELEGRAM_POOL_TIMEOUT", "15")),
            )
            bot = Bot(token=token, request=request)
            try:
                with output.open("rb") as handle:
                    return await bot.send_video(
                        chat_id=int(chat_id),
                        message_thread_id=int(topic_id),
                        video=handle,
                        duration=duration,
                        width=width,
                        height=height,
                        caption=self._publication_text(item),
                        supports_streaming=True,
                    )
            finally:
                await bot.shutdown()

        self.db.publication_send_started(item.item_id)

        try:
            message = self._run_async(send())
        except (TimedOut, NetworkError) as exc:
            status = self.check_publication(item)
            if status == PublicationCheck.CONFIRMED:
                publication = self._publication(item)
                message_id = publication["published_message_id"] if publication else None
                if message_id:
                    return PublicationResult(True, str(message_id))
            raise PublicationUnknownError("Telegram publication outcome is UNKNOWN") from exc

        message_id = getattr(message, "message_id", None)
        if message_id is None:
            raise RuntimeError("Telegram não retornou message_id")

        self.db.publication_message_sent(item.item_id, str(message_id))

        if os.getenv("ARMORED_TEST_CRASH_AFTER_TELEGRAM_SEND", "0") == "1":
            raise RuntimeError("TEST_CRASH_AFTER_TELEGRAM_SEND")

        self.db.publication_confirmed(item.item_id, str(message_id))
        return PublicationResult(True, str(message_id))


def build(root: Path, db: Database | None = None, **_kwargs):
    return ArmoredHub(root, db)
