from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .database import Database
from .models import State
from .pipeline import Pipeline
from .recovery import Recovery
from .services import IngestMessage, SyncService
from .startup_audit import StartupReconciler
from .storage import Storage


class Coordinator:
    """Single composition root for the isolated ArmoredCreator pipeline."""

    def __init__(self, db, storage, vision, studio, publisher, source=None, ia=None):
        self.db = db
        self.storage = storage
        self.sync = SyncService(db, storage)
        self.pipeline = Pipeline(db, storage, vision, studio, publisher, ia)
        self.recovery = Recovery(db, storage, vision, studio, publisher, ia)
        self.startup_reconciler = StartupReconciler(db, storage, publisher)
        self.source = source
        self._runtime_lock_held = False
        self._last_catch_up_completed_count = 0
        self._shutdown_requested = False
        self.pipeline.set_shutdown_checker(lambda: self._shutdown_requested)

    @classmethod
    def build(cls, root: Path | None = None, bindings: Any | None = None):
        storage = Storage(root)
        # Project-local credential source of truth.
        # Secrets live only in credentials/project.env; runtime modules continue
        # consuming them through os.getenv() and do not know the file location.
        project_credentials = storage.root / "credentials" / "project.env"
        if project_credentials.exists():
            load_dotenv(project_credentials, override=True)

        # .env contains project configuration, not secrets. Keep externally
        # supplied configuration compatible while preventing it from replacing
        # the project credential source above.
        project_config = storage.root / ".env"
        if project_config.exists():
            load_dotenv(project_config, override=False)

        db = Database(storage.database / "armoredcreator.db")
        if bindings is None:
            from ArmoredHub.service import ArmoredHub
            from ArmoredStudio.service import ArmoredStudio
            from ArmoredVision.service import ArmoredVision
            from ArmoredIA.service import ArmoredIA
            from ArmoredSync.service import LocalSource, TelegramReader, TelegramSource

            vision = ArmoredVision()
            ia = ArmoredIA()
            studio = ArmoredStudio(storage.root)
            publisher = ArmoredHub(storage.root, db)
            if os.getenv("ARMORED_REAL_TELEGRAM", "0") == "1":
                api_id = os.getenv("TELEGRAM_API_ID")
                api_hash = os.getenv("TELEGRAM_API_HASH")
                if not api_id or not api_hash:
                    raise RuntimeError("TELEGRAM_API_ID e TELEGRAM_API_HASH são obrigatórios")
                reader = TelegramReader(storage.root, int(api_id), api_hash)
                source = TelegramSource(storage.root, reader, db)
            else:
                source = LocalSource(storage.root / "input")
            return cls(db, storage, vision, studio, publisher, source, ia)
        return cls(db, storage, bindings.vision, bindings.studio, bindings.publisher, bindings.source, getattr(bindings, "ia", None))

    async def _ensure_source_connection(self) -> None:
        """Reconnect a real Telegram source before materializing the next item."""
        source = self.source
        reader = getattr(source, "reader", None)
        connect = getattr(reader, "connect", None)
        if connect is not None:
            is_connected = getattr(reader, "is_connected", None)
            if callable(is_connected):
                if is_connected():
                    return
            else:
                client = getattr(reader, "client", None)
                client_is_connected = getattr(client, "is_connected", None)
                if callable(client_is_connected) and client_is_connected():
                    return
            await connect()

    async def _release_source_connection(self) -> None:
        """Release a real Telegram Sync session before Hub opens the same session.

        Telethon stores the user session in SQLite. Keeping ArmoredSync connected
        while ArmoredHub opens that same session causes a Windows SQLite lock.
        Ingestion/materialization is complete before this method is called, so
        releasing the connection does not interrupt the canonical pipeline.
        """
        source = self.source
        reader = getattr(source, "reader", None)
        disconnect = getattr(reader, "disconnect", None)
        if disconnect is not None:
            await disconnect()
            return
        # Non-Telegram lab sources may expose a convenience disconnect()
        # method without owning a real Sync session. Do not call it blindly:
        # the Coordinator must release only the connection it actually manages.
        # Real Telegram ownership is represented by source.reader above.

    async def ingest_once_async(self):
        if self.source is None:
            raise RuntimeError("Sync source não configurado")
        fetch_async = getattr(self.source, "fetch_next_async", None)
        message = await fetch_async() if fetch_async is not None else self.source.fetch_next()
        if message is None:
            await self._release_source_connection()
            return None
        item_id = await self.sync.ingest_message_async(IngestMessage(
            telegram_message_id=str(message.telegram_message_id),
            source_id=getattr(message, "source_id", "telegram"),
            topic_id=getattr(message, "topic_id", None),
            topic_name=getattr(message, "topic_name", None),
            original_url=getattr(message, "original_url", None),
            source_path=getattr(message, "source_path", None),
            materialize=getattr(message, "materialize", None),
        ))
        marker = getattr(self.source, "mark_ingested", None)
        if marker is not None:
            marker(str(message.telegram_message_id))
        await self._release_source_connection()
        return item_id

    def ingest_once(self):
        if self.source is None:
            raise RuntimeError("Sync source não configurado")
        if getattr(self.source, "fetch_next_async", None) is not None:
            import asyncio
            return asyncio.run(self.ingest_once_async())
        message = self.source.fetch_next()
        if message is None:
            return None
        item_id = self.sync.ingest_message(IngestMessage(
            telegram_message_id=str(message.telegram_message_id),
            source_id=getattr(message, "source_id", "telegram"),
            topic_id=getattr(message, "topic_id", None),
            topic_name=getattr(message, "topic_name", None),
            original_url=getattr(message, "original_url", None),
            source_path=getattr(message, "source_path", None),
            materialize=getattr(message, "materialize", None),
        ))
        marker = getattr(self.source, "mark_ingested", None)
        if marker is not None:
            marker(str(message.telegram_message_id))
        reader = getattr(self.source, "reader", None)
        disconnect = getattr(reader, "disconnect", None)
        if disconnect is not None:
            import asyncio
            asyncio.run(disconnect())
        return item_id

    async def _vision_gate_and_materialize_async(self, message):
        """Reserve candidate, run Vision, then materialize only when accepted."""
        ingest = IngestMessage(
            telegram_message_id=str(message.telegram_message_id),
            source_id=getattr(message, "source_id", "telegram"),
            topic_id=getattr(message, "topic_id", None),
            topic_name=getattr(message, "topic_name", None),
            original_url=getattr(message, "original_url", None),
            source_path=getattr(message, "source_path", None),
            materialize=getattr(message, "materialize", None),
        )
        # Test doubles/subclasses that override Coordinator.run() are part of
        # the original lifecycle contract: let them receive a normally materialized
        # candidate instead of forcing the new Vision gate through their override.
        if getattr(self.run, "__func__", None) is not Coordinator.run:
            await self._ensure_source_connection()
            try:
                item_id = str(await self.sync.ingest_message_async(ingest))
                return item_id, True, self.db.get(item_id)
            finally:
                await self._release_source_connection()

        item_id = str(self.sync.reserve_message(ingest))
        try:
            # Match the original Coordinator lifecycle: Sync owns the source
            # connection while the single candidate is reserved and materialized.
            # Vision itself is URL-only, so no media is downloaded here.
            await self._ensure_source_connection()
            current = self.db.get(item_id)

            # A previous Vision-approved candidate may have failed during
            # materialization. It remains RECEIVED with durable affiliate_url
            # but without ORIGINAL. On rediscovery, materialize first; never let
            # Pipeline.run() jump directly to Studio against a missing ORIGINAL.
            if current.affiliate_url and not current.original_path.is_file():
                await self.sync.materialize_message_async(ingest)
                return item_id, True, self.db.get(item_id)

            if current.state != State.FAILED:
                self.pipeline.run(item_id, stop_after_vision=True)
            current = self.db.get(item_id)
            if current.state == State.WAITING_VISION:
                return item_id, False, current

            # stop_after_vision returns the accepted candidate to RECEIVED with
            # durable Vision evidence. Materialize only now, after the gate.
            if not current.affiliate_url:
                return item_id, False, current
            if not current.original_path.is_file():
                await self.sync.materialize_message_async(ingest)
                return item_id, True, self.db.get(item_id)
            return item_id, True, current
        except Exception:
            # A technical Vision failure happens before the materialization
            # phase and may still need the immutable original for Recovery.
            # Once Vision has durably accepted the candidate (affiliate_url),
            # a failure here is the Sync materialization failure itself: do not
            # retry the download inline. Leave RECEIVED without the original
            # so the recovery wrapper can perform its single same-run source
            # rediscovery through the durable checkpoint.
            # Vision is now a hard pre-download gate. A technical Vision
            # failure is durable RECOVERY, but it must never trigger a download
            # merely to make Recovery possible. Recovery/source rediscovery will
            # retry the candidate from Telegram without materializing media first.
            raise
        finally:
            # Sync must be released before Studio/Hub can use the same session.
            await self._release_source_connection()

    async def run_catch_up_async(self) -> list[str]:
        """Discover, materialize, release Sync, and process exactly one item at a time.

        CATCH-UP deliberately does not collect a materialized batch. The Sync
        source only exposes the next eligible candidate; that candidate is
        materialized into its canonical workspace, the Telegram session is
        released, and only then does Vision/Studio/Hub run.
        """
        processed: list[str] = []
        completed_count = 0
        checkpoint_blocked = False
        source = self.source
        bounded_limit = getattr(source, "_historical_limit", None)
        fetch_next = getattr(source, "fetch_next_async", None)
        if fetch_next is None:
            raise RuntimeError("Sync source não implementa fetch_next_async")

        while True:
            message = await fetch_next()
            if message is None:
                failed = checkpoint_blocked or bool(
                    getattr(source, "historical_materialization_failed", False)
                )
                scan_complete = bool(
                    getattr(source, "historical_scan_exhausted", True)
                )
                if scan_complete and not failed:
                    complete = getattr(source, "complete_historical_sync", None)
                    if complete is not None:
                        complete()
                    else:
                        self.db.complete_historical_sync()
                break

            item_id = str(message.telegram_message_id)

            # A historical candidate can be rediscovered when the persisted
            # checkpoint is still behind it (for example after an interrupted
            # certification run). A previously completed item must never be
            # counted as part of the current bounded certification.
            try:
                existing = self.db.get(item_id)
            except KeyError:
                existing = None
            if (
                existing is not None
                and existing.state == State.PUBLISHED
                and existing.cleanup_completed
            ):
                marker = getattr(source, "mark_ingested", None)
                if marker is not None:
                    marker(item_id)

                # A previously completed item may be rediscovered after an
                # earlier candidate was recovered. Its checkpoint is safe to
                # advance now because this item is durably PUBLISHED+cleanup.
                topic_id = getattr(message, "topic_id", None)
                commit = getattr(source, "commit_live_checkpoints", None)
                if topic_id is not None and commit is not None:
                    commit({int(topic_id): int(item_id)})
                continue

            materialized = False
            current = None
            try:
                item_id, materialized, current = await self._vision_gate_and_materialize_async(message)
                marker = getattr(source, "mark_ingested", None)
                if marker is not None:
                    marker(str(message.telegram_message_id))
            except Exception as exc:
                checkpoint_blocked = True
                marker = getattr(source, "mark_materialization_failed", None)
                if marker is not None:
                    marker()
                import logging
                logging.getLogger(__name__).exception(
                    "[COORDINATOR][CATCH-UP] Vision/materialization failed for %s; checkpoint blocked: %s",
                    getattr(message, "telegram_message_id", "?"), exc,
                )
                # The gate may fail after reserving the candidate (for example
                # during Vision or materialization). Re-read durable state so
                # the lifecycle handler never dereferences a missing local value.
                try:
                    current = self.db.get(item_id)
                except Exception:
                    current = None

            if not materialized:
                if current is not None and current.state == State.WAITING_VISION:
                    topic_id = getattr(message, "topic_id", None)
                    commit = getattr(source, "commit_live_checkpoints", None)
                    if topic_id is not None and commit is not None and not checkpoint_blocked:
                        commit({int(topic_id): int(message.telegram_message_id)})
                    processed.append(item_id)
                continue

            # Exactly one item crosses the Sync -> Pipeline boundary.
            try:
                if self.db.get(item_id).state != State.FAILED:
                    self.run(item_id)
                    current = self.db.get(item_id)

                    # RECOVERY is a technical/incomplete-processing state:
                    # it keeps the historical checkpoint blocked until the item
                    # is resolved. WAITING_VISION is different: Vision has
                    # durably classified the candidate but found no Shopee
                    # destination. It is retained in SQLite and does not block
                    # historical progress or LIVE.
                    if current.state == State.RECOVERY:
                        checkpoint_blocked = True
                        processed.append(item_id)
                        continue

                    if current.state == State.WAITING_VISION:
                        topic_id = getattr(message, "topic_id", None)
                        commit = getattr(source, "commit_live_checkpoints", None)
                        if topic_id is not None and commit is not None and not checkpoint_blocked:
                            commit({int(topic_id): int(message.telegram_message_id)})
                        processed.append(item_id)
                        continue

                    if (
                        current.state == State.PUBLISHED
                        and current.cleanup_completed
                        and not checkpoint_blocked
                    ):
                        # Checkpoints are monotonic and must never jump past
                        # an earlier candidate whose materialization or
                        # processing failed. WAITING_VISION is not such a
                        # blocker: it is a durable "no destination for now"
                        # classification and may advance its topic checkpoint.
                        topic_id = getattr(message, "topic_id", None)
                        commit = getattr(source, "commit_live_checkpoints", None)
                        if topic_id is not None and commit is not None:
                            commit({int(topic_id): int(message.telegram_message_id)})
            except Exception as exc:
                checkpoint_blocked = True
                import logging
                logging.getLogger(__name__).exception(
                    "[COORDINATOR][CATCH-UP] Falha no processamento de %s; "
                    "checkpoint permanece no último item confirmado: %s",
                    item_id,
                    exc,
                )

                # Pipeline.run() persists recoverable failures as RECOVERY and
                # then re-raises. Keep the failed candidate durable, but continue
                # the historical scan. Its checkpoint remains blocked until a
                # later recovery pass resolves the technical failure.
                current = self.db.get(item_id)
                if current.state == State.RECOVERY:
                    # Technical pipeline failures are durable RECOVERY items,
                    # but they must not abort the historical scan. The current
                    # checkpoint remains blocked; later candidates may be
                    # processed one at a time. A later recovery pass revisits
                    # this item before LIVE is allowed.
                    processed.append(item_id)
                    continue

            # Count only a fully published and cleaned item.
            current = self.db.get(item_id)
            # "processed" means a candidate completed its pipeline attempt and
            # is retained for lifecycle diagnostics/tests. Certification limits
            # are intentionally based only on fully published + cleaned items.
            processed.append(item_id)
            if current.state == State.PUBLISHED and current.cleanup_completed:
                completed_count += 1

            if bounded_limit is not None and completed_count >= bounded_limit:
                import logging
                logging.getLogger(__name__).info(
                    "[COORDINATOR][CATCH-UP] Limite fechado atingido: %s item(ns). "
                    "Devolvendo controle ao Coordinator para encerramento ou cutover de certificação.",
                    completed_count,
                )
                self._last_catch_up_completed_count = completed_count
                return processed

        self._last_catch_up_completed_count = completed_count
        return processed

    def run_catch_up(self) -> list[str]:
        import asyncio
        processed = asyncio.run(self.run_catch_up_async())
        # Never force LIVE here. The async runner is the authority: unresolved
        # technical failures leave historical sync open while later candidates
        # may still be processed; recovery remains responsible for the blocked
        # checkpoint before LIVE.
        return processed

    async def _fetch_live_candidate_with_watchdog(self, fetch_candidate):
        """Bound one LIVE Telegram discovery operation and force a clean reconnect on stall."""
        try:
            timeout = max(
                0.1,
                float(os.getenv("ARMORED_LIVE_DISCOVERY_TIMEOUT", "30")),
            )
        except ValueError:
            timeout = 30.0

        try:
            return await asyncio.wait_for(fetch_candidate(), timeout=timeout)
        except asyncio.TimeoutError:
            import logging

            logging.getLogger(__name__).error(
                "[COORDINATOR][LIVE] Descoberta Telegram excedeu %.1fs; "
                "forçando desconexão para permitir reconexão limpa no próximo ciclo.",
                timeout,
            )
            try:
                await self._release_source_connection()
            except Exception as release_exc:
                logging.getLogger(__name__).warning(
                    "[COORDINATOR][LIVE] Falha ao liberar sessão após timeout: %s",
                    release_exc,
                )
            raise

    async def run_live_once_async(self) -> list[str]:
        """Discover, materialize, release Sync, and process exactly one LIVE item."""
        source = self.source
        fetch_candidate = getattr(source, "fetch_live_candidate_async", None)

        if fetch_candidate is not None:
            message, checkpoints = await self._fetch_live_candidate_with_watchdog(
                fetch_candidate
            )
            messages = [] if message is None else [message]
        else:
            # Compatibility for lab sources that still expose the old method.
            fetch_batch = getattr(source, "fetch_live_batch_async", None)
            if fetch_batch is None:
                return []
            try:
                messages, checkpoints = await fetch_batch(limit=1)
            except TypeError as exc:
                if "limit" not in str(exc):
                    raise
                messages, checkpoints = await fetch_batch()
                messages = messages[:1]
                checkpoints = dict(checkpoints) if messages else {}

        if not messages:
            commit = getattr(source, "commit_live_checkpoints", None)
            if commit is not None and checkpoints:
                commit(checkpoints)
            return []

        message = messages[0]
        item_id = str(message.telegram_message_id)
        materialized = False
        current = None
        try:
            item_id, materialized, current = await self._vision_gate_and_materialize_async(message)
            marker = getattr(source, "mark_ingested", None)
            if marker is not None:
                marker(str(message.telegram_message_id))
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception(
                "[COORDINATOR][LIVE] Vision/materialization failed for %s; checkpoint blocked: %s",
                getattr(message, "telegram_message_id", "?"), exc,
            )
            try:
                current = self.db.get(str(item_id)) if item_id is not None else None
            except Exception:
                current = None

        if not materialized:
            if current is not None and current.state == State.WAITING_VISION:
                commit = getattr(source, "commit_live_checkpoints", None)
                if commit is not None and checkpoints:
                    commit(checkpoints)
                return [str(item_id)]
            if item_id is not None:
                # The candidate was durably reserved and attempted; even a
                # recoverable Vision/materialization failure belongs to this
                # poll result and must not disappear from lifecycle diagnostics.
                return [str(item_id)]
            return []

        # Materialization alone never advances the source checkpoint.
        # The item must complete the full pipeline and cleanup first.
        try:
            if self.db.get(str(item_id)).state != State.FAILED:
                self.run(str(item_id))
                current = self.db.get(str(item_id))

                # An unresolved RECOVERY item blocks the next LIVE polling
                # cycle. Reconcile once now; UNKNOWN remains durable and must
                # never be followed by another publication candidate.
                if current.state == State.RECOVERY:
                    try:
                        self.recover(str(item_id))
                    except Exception as recovery_exc:
                        import logging
                        logging.getLogger(__name__).warning(
                            "[COORDINATOR][LIVE] Item %s permanece em RECOVERY; "
                            "próximo ciclo será bloqueado: %s",
                            item_id,
                            recovery_exc,
                        )
                    current = self.db.get(str(item_id))

                if current.state == State.PUBLISHED and current.cleanup_completed:
                    commit = getattr(source, "commit_live_checkpoints", None)
                    if commit is not None and checkpoints:
                        commit(checkpoints)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception(
                "[COORDINATOR][LIVE] Falha no processamento de %s; checkpoint "
                "permanece no último item confirmado: %s",
                item_id,
                exc,
            )
        return [str(item_id)]

    def run_live_once(self) -> list[str]:
        import asyncio
        return asyncio.run(self.run_live_once_async())

    @staticmethod
    def _is_transient_source_error(exc: BaseException) -> bool:
        """Return whether an origin failure is safe to retry inside CATCH-UP."""
        return isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError))

    async def _run_catch_up_with_recovery_async(self) -> None:
        """Run historical processing, then retry technical RECOVERY items.

        A technical failure must not starve the rest of CATCH-UP. Its checkpoint
        remains blocked, so after the historical scan we reconcile RECOVERY
        items and, when progress is made, rescan from the durable checkpoint.
        WAITING_VISION is intentionally not auto-retried here.

        Real Telegram connection failures can also happen while the historical
        iterator is being advanced after a materialization failure. In that
        case the iterator is no longer safe to resume. Release the source,
        rebuild its historical iterator from the durable checkpoint, and retry
        with bounded backoff so the Coordinator process remains alive.
        """
        rediscovery_attempted: set[str] = set()
        source_error_backoff = max(
            1.0,
            float(os.getenv("ARMORED_SYNC_ERROR_BACKOFF", "5")),
        )
        while True:
            try:
                await self.run_catch_up_async()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._is_transient_source_error(exc) or not hasattr(
                    self.source, "reader"
                ):
                    raise
                import logging
                logging.getLogger(__name__).exception(
                    "[COORDINATOR][CATCH-UP] Telegram/rede de origem indisponível; "
                    "Coordinator permanece vivo e tentará reconectar: %s",
                    exc,
                )
                try:
                    await self._release_source_connection()
                except Exception as release_exc:
                    logging.getLogger(__name__).warning(
                        "[COORDINATOR][CATCH-UP] Falha ao liberar sessão após "
                        "erro de origem: %s",
                        release_exc,
                    )
                reset = getattr(self.source, "reset_historical_scan", None)
                if reset is None:
                    raise
                reset()
                await asyncio.sleep(source_error_backoff)
                continue
            # Historical completion alone is not enough to stop recovery:
            # a materialization failure may leave a durable RECEIVED reservation
            # without its immutable original while the source reports its scan
            # exhausted. Inspect the durable recovery candidates first.
            before = set()
            for row in self.db.conn.execute(
                "SELECT content_id, state FROM items "
                "WHERE state IN (?, ?, ?) ORDER BY created_at, content_id",
                (State.RECOVERY.value, State.RECEIVED.value, State.VISION.value),
            ).fetchall():
                item = self.db.get(str(row["content_id"]))
                if (
                    item.state == State.RECOVERY
                    or (
                        item.state in (State.RECEIVED, State.VISION)
                        and item.affiliate_url
                        and not item.original_path.is_file()
                    )
                ):
                    before.add(str(row["content_id"]))
            if not before:
                return

            # Pre-download candidates without an immutable original cannot be
            # recovered from SQLite alone. They are durable reservations created by
            # the Vision gate (RECEIVED/VISION) or technical Vision failures (RECOVERY),
            # so give each one its source rediscovery attempt before generic Recovery
            # can touch the remaining candidates. This is deliberately done after
            # the historical scan is exhausted: the source checkpoint must never
            # be reset while the current scan still has unseen candidates.
            source_rediscovery_missing = {
                item_id
                for item_id in before
                if not self.db.get(item_id).original_path.is_file()
                and (
                    self.db.get(item_id).state == State.RECOVERY
                    or (
                        self.db.get(item_id).state in (State.RECEIVED, State.VISION)
                        and self.db.get(item_id).affiliate_url
                    )
                )
            }
            pending_rediscovery = source_rediscovery_missing - rediscovery_attempted
            if pending_rediscovery:
                rediscovery_attempted.update(pending_rediscovery)
                reset = getattr(self.source, "reset_historical_scan", None)
                if reset is None:
                    return
                reset()
                continue

            self.recover_pending()

            after = set()
            for row in self.db.conn.execute(
                "SELECT content_id, state FROM items "
                "WHERE state IN (?, ?, ?) ORDER BY created_at, content_id",
                (State.RECOVERY.value, State.RECEIVED.value, State.VISION.value),
            ).fetchall():
                item = self.db.get(str(row["content_id"]))
                if (
                    item.state == State.RECOVERY
                    or (
                        item.state in (State.RECEIVED, State.VISION)
                        and item.affiliate_url
                        and not item.original_path.is_file()
                    )
                ):
                    after.add(str(row["content_id"]))

            # When Recovery makes progress, the Sync iterator must be rebuilt
            # from the durable checkpoint. The real Telegram source otherwise
            # remains exhausted and its in-memory _seen set would hide the
            # already-scanned candidate on the same process lifetime.
            if not before.issubset(after):
                reset = getattr(self.source, "reset_historical_scan", None)
                if reset is None:
                    return
                reset()
                continue

            # No Recovery/rediscovery progress: stop instead of tight-looping.
            return

    async def _run_forever_async(
        self,
        poll_seconds: float = 2.0,
        max_cycles: int | None = None,
    ) -> None:
        """Run the complete Coordinator lifetime inside one asyncio event loop.

        Telethon binds its client to the event loop used at connection time.
        The previous implementation called ``asyncio.run()`` for every
        CATCH-UP/LIVE operation, creating a new loop on every cycle.
        That is incompatible with a persistent Telethon session and can
        surface as ``The asyncio event loop must not change after connection``.
        """
        # Startup is a separate reconciliation phase. It inventories SQLite +
        # canonical storage and reconciles ambiguous publications before Sync
        # opens its Telegram session. It never downloads or publishes.
        self.startup_reconciler.run()
        self.recover_pending()

        if (
            not self.db.historical_complete()
            or not self.db.has_sync_checkpoints()
        ):
            if self.db.historical_complete() and not self.db.has_sync_checkpoints():
                self.db.set_sync_mode("CATCH_UP")
            await self._run_catch_up_with_recovery_async()

            # A bounded CATCH-UP run remains opt-in certification behavior.
            # By default it terminates here, preserving the existing contract.
            bounded_limit = getattr(self.source, "_historical_limit", None)
            cert_then_live = (
                os.getenv("ARMORED_CERT_CATCHUP_THEN_LIVE", "0").strip() == "1"
            )
            bounded_completed = (
                bounded_limit is not None
                and self._last_catch_up_completed_count >= int(bounded_limit)
            )

            # An unresolved historical candidate blocks the checkpoint and must
            # never fall through into LIVE. The only exception is the explicit
            # bounded certification flow, which first performs the source's
            # durable history-to-LIVE cutover.
            if not self.db.historical_complete():
                if bounded_completed and cert_then_live:
                    cutover = getattr(self.source, "prepare_live_cutover_async", None)
                    if cutover is None:
                        raise RuntimeError(
                            "ARMORED_CERT_CATCHUP_THEN_LIVE=1 exige que a fonte "
                            "implemente prepare_live_cutover_async()"
                        )
                    import logging
                    await cutover()
                    logging.getLogger(__name__).info(
                        "[COORDINATOR][CERT] CATCH-UP limitado concluído; "
                        "cutover histórico seguro executado; entrando em LIVE"
                    )

                if not self.db.historical_complete():
                    import logging
                    logging.getLogger(__name__).warning(
                        "[COORDINATOR][CATCH-UP] Histórico ainda não concluído; "
                        "LIVE bloqueado. O processo permanecerá encerrado até a "
                        "reconciliação/reexecução do candidato pendente."
                    )
                    return

            if bounded_completed and not cert_then_live:
                return

        import logging
        logging.getLogger(__name__).info(
            "[COORDINATOR][LIVE] CATCH-UP concluído; Coordinator entrou em modo LIVE "
            "(monitoramento contínuo iniciado)"
        )

        cycles = 0
        live_error_backoff = max(
            1.0,
            float(os.getenv("ARMORED_LIVE_ERROR_BACKOFF", "5")),
        )
        while max_cycles is None or cycles < max_cycles:
            self.db.heartbeat_runtime_lock("coordinator")
            try:
                processed = await self.run_live_once_async()
                cycles += 1
                if not processed and (max_cycles is None or cycles < max_cycles):
                    await asyncio.sleep(float(poll_seconds))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Telegram/network outages are transient LIVE conditions, not
                # process-fatal errors. The source deliberately leaves the
                # checkpoint unchanged when discovery/materialization fails,
                # so the same candidate can be rediscovered after reconnect.
                # Keep the Coordinator alive and let the next iteration rebuild
                # the Telegram client/session through _ensure_source_connection.
                import logging
                logging.getLogger(__name__).exception(
                    "[COORDINATOR][LIVE] Telegram/rede de origem indisponível; "
                    "Coordinator permanece vivo e tentará reconectar: %s",
                    exc,
                )
                cycles += 1
                if max_cycles is None or cycles < max_cycles:
                    await asyncio.sleep(live_error_backoff)

    def run_forever(self, poll_seconds: float = 2.0, max_cycles: int | None = None) -> None:
        """Recover, finish catch-up once, then monitor Telegram continuously.

        The entire production lifetime is executed under one asyncio loop.
        This is required by Telethon and also makes LIVE restart behavior
        deterministic.
        """
        import asyncio
        self.db.acquire_runtime_lock("coordinator")
        self._runtime_lock_held = True
        self._shutdown_requested = False
        previous_sigint = signal.getsignal(signal.SIGINT)

        def _request_shutdown(signum, frame):
            # Mark shutdown before an interrupted blocking call returns. Child
            # tools such as RVC/FFmpeg may turn Ctrl+C into a non-zero exit code.
            self._shutdown_requested = True

        signal.signal(signal.SIGINT, _request_shutdown)
        try:
            asyncio.run(
                self._run_forever_async(
                    poll_seconds=poll_seconds,
                    max_cycles=max_cycles,
                )
            )
        finally:
            signal.signal(signal.SIGINT, previous_sigint)
            if self._runtime_lock_held:
                self.db.release_runtime_lock("coordinator")
                self._runtime_lock_held = False
    def run(self, item_id: str) -> None:
        self.pipeline.run(item_id)

    def recover(self, item_id: str) -> None:
        self.recovery.reconcile(item_id)

    def close(self) -> None:
        if self._runtime_lock_held:
            self.db.release_runtime_lock("coordinator")
            self._runtime_lock_held = False
        self.db.close()

    def process_next(self):
        item_id = self.ingest_once()
        if item_id is None:
            return None
        self.run(item_id)
        return item_id

    def recover_pending(self):
        states = (
            State.RECEIVED.value, State.VISION.value, State.IA.value,
            State.STUDIO.value, State.PUBLISHING.value, State.RECOVERY.value,
            State.FAILED.value, State.PUBLISHED.value,
        )
        placeholders = ",".join("?" for _ in states)
        rows = self.db.conn.execute(
            f"SELECT content_id, state FROM items WHERE state IN ({placeholders}) "
            "OR (state=? AND cleanup_completed=0) ORDER BY created_at, content_id",
            (*states, State.PUBLISHED.value),
        ).fetchall()
        recovered = []
        for row in rows:
            item_id = str(row["content_id"])

            # FAILED is a legacy terminal state from the previous
            # generic-exception path. Current processing failures are persisted
            # as RECOVERY; legacy FAILED rows must therefore be reopened once
            # through the same deterministic recovery path instead of being
            # silently abandoned. Recovery itself decides the correct resume
            # stage from durable state/artifacts.
            if str(row["state"]) == State.FAILED.value:
                self.db.transition(item_id, State.RECOVERY, "legacy-failed-recovery")
            # RECEIVED without an immutable original is a durable Telegram
            # reservation whose download was interrupted. The Sync source must
            # rediscover/materialize it; Recovery cannot invent the missing
            # bytes. Keep it in SQLite and let CATCH_UP/LIVE continue it.
            if str(row["state"]) == State.RECEIVED.value:
                item = self.db.get(item_id)
                if not item.original_path.is_file():
                    # An interrupted download leaves only the transactional
                    # .part artifact. Sync always restarts materialization
                    # from Telegram, so never treat the partial as a usable
                    # original. Remove it before rediscovery to keep the
                    # canonical workspace deterministic.
                    partial = item.original_path.with_suffix(
                        item.original_path.suffix + ".part"
                    )
                    if partial.exists():
                        try:
                            partial.unlink()
                        except OSError as exc:
                            import logging
                            logging.getLogger(__name__).warning(
                                "[STARTUP][DOWNLOAD] id=%s não foi possível remover .part: %s",
                                item_id, exc,
                            )
                    continue
            try:
                self.recover(item_id)
                current = self.db.get(item_id)
                recovered.append(item_id)
                if (
                    current.state in (State.WAITING_VISION, State.RECOVERY)
                    or (current.state == State.PUBLISHED and not current.cleanup_completed)
                ):
                    # Startup recovery is ordered. An unresolved current item,
                    # including PUBLISHED with cleanup still pending, blocks
                    # progression because its durable lifecycle is incomplete.
                    break
            except Exception as exc:
                import logging
                current = self.db.get(item_id)
                logging.getLogger(__name__).exception(
                    "Recovery falhou para item %s; state=%s: %s",
                    item_id,
                    current.state.value,
                    exc,
                )
                if (
                    current.state in (State.WAITING_VISION, State.RECOVERY)
                    or (current.state == State.PUBLISHED and not current.cleanup_completed)
                ):
                    # The current candidate still owns the checkpoint. Never
                    # continue startup recovery with a later item while this
                    # candidate remains unresolved or cleanup-pending.
                    break
        return recovered