from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from .models import State
from .services import IngestMessage

log = logging.getLogger(__name__)

VALID_STAGES = ("sync", "vision", "stock")


def _source_order(source: Any) -> dict[str, int]:
    return {
        str(candidate.source_id): index
        for index, candidate in enumerate(getattr(source, "sources", ()))
        if getattr(candidate, "source_id", None) is not None
    }


def _ordered(items: list[Any], source: Any) -> list[Any]:
    rank = _source_order(source)
    return sorted(
        items,
        key=lambda item: (
            rank.get(str(item.source_id), len(rank)),
            int(item.telegram_message_id)
            if str(item.telegram_message_id).isdigit()
            else 0,
            str(item.content_id),
        ),
    )


async def run_catch_up_stage(coordinator: Any, stage: str) -> dict[str, Any]:
    """Execute exactly one historical catch-up stage, then return to the operator.

    This entry point deliberately does not run Recovery, Studio, IA, Hub,
    publication, cleanup, LIVE cutover, or historical checkpoint completion.
    SQLite reservations and Vision evidence are the restart boundary.
    """
    stage = str(stage).strip().lower()
    if stage not in VALID_STAGES:
        raise ValueError(f"stage inválido: {stage}; use um de {', '.join(VALID_STAGES)}")

    source = coordinator.source
    if source is None:
        raise RuntimeError("Sync source não configurado")
    source_list = tuple(getattr(source, "sources", ()))
    if len(source_list) < 3:
        raise RuntimeError(
            "Execução por etapas exige as três fontes configuradas em project.env; "
            f"foram encontradas {len(source_list)}."
        )

    per_source: Counter[str] = Counter(
        {str(candidate.source_id): 0 for candidate in source_list}
    )
    errors: list[str] = []
    processed: list[str] = []
    skipped_existing_original = 0

    if stage == "sync":
        iterator_factory = getattr(source, "iter_historical_candidates_async", None)
        if not callable(iterator_factory):
            raise RuntimeError("Sync source não implementa iter_historical_candidates_async()")
        try:
            async for message in iterator_factory():
                source_id = str(getattr(message, "source_id", "") or "")
                message_id = str(message.telegram_message_id)
                try:
                    ingest = IngestMessage(
                        telegram_message_id=message_id,
                        source_id=source_id,
                        topic_id=getattr(message, "topic_id", None),
                        topic_name=getattr(message, "topic_name", None),
                        original_url=getattr(message, "original_url", None),
                        source_path=getattr(message, "source_path", None),
                        materialize=getattr(message, "materialize", None),
                    )
                    item_id = str(coordinator.sync.reserve_message(ingest))
                    marker = getattr(source, "mark_ingested", None)
                    if callable(marker):
                        marker(message_id)
                    per_source[source_id] += 1
                    processed.append(item_id)
                except Exception as exc:
                    errors.append(f"sync:{source_id}:{message_id}:{exc}")
                    log.error(
                        "[CATCH-UP][SYNC] Falha source=%s mensagem=%s: %s",
                        source_id,
                        message_id,
                        exc,
                    )
        finally:
            await coordinator._release_source_connection()

        scan_complete = bool(getattr(source, "historical_scan_exhausted", False))
        limited = bool(
            getattr(source, "historical_limit_reached", False)
            or getattr(source, "historical_collection_limited", False)
        )
        if not scan_complete:
            errors.append("sync:varredura histórica não chegou ao fim")
        if limited:
            errors.append("sync:limite de coleta configurado; histórico não pode ser declarado completo")

    elif stage == "vision":
        # Recover the narrow crash window after Vision evidence is committed
        # but before Pipeline transitions VISION -> RECEIVED.
        interrupted_approved = coordinator.db.vision_approved_interrupted_items()
        for item in _ordered(interrupted_approved, source):
            coordinator.db.transition(
                item.content_id,
                State.RECEIVED,
                "catch-up-recovered-vision-evidence",
            )
        candidates = _ordered(coordinator.db.pending_vision_candidates(), source)
        for item in candidates:
            try:
                coordinator.pipeline.run(item.content_id, stop_after_vision=True)
                current = coordinator.db.get(item.content_id)
                per_source[str(current.source_id)] += 1
                processed.append(item.content_id)
                if current.state == State.RECOVERY:
                    errors.append(f"vision:{item.content_id}:falha técnica em RECOVERY")
            except Exception as exc:
                errors.append(f"vision:{item.content_id}:{exc}")
                log.error(
                    "[CATCH-UP][VISION] Falha item=%s: %s",
                    item.content_id,
                    exc,
                )
        # WAITING_VISION is a durable, expected outcome, not a technical failure.
        waiting = coordinator.db.conn.execute(
            "SELECT COUNT(*) FROM items WHERE state=?",
            (State.WAITING_VISION.value,),
        ).fetchone()[0]
        approved = coordinator.db.conn.execute(
            "SELECT COUNT(*) FROM items WHERE state=? "
            "AND affiliate_url IS NOT NULL AND TRIM(affiliate_url)<>''",
            (State.RECEIVED.value,),
        ).fetchone()[0]
        retryable = coordinator.db.conn.execute(
            "SELECT COUNT(*) FROM items WHERE state=? "
            "AND (affiliate_url IS NULL OR TRIM(affiliate_url)='')",
            (State.VISION.value,),
        ).fetchone()[0]
        log.info(
            "[CATCH-UP][VISION] aprovados=%s aguardando=%s retryable=%s",
            approved,
            waiting,
            retryable,
        )

    else:  # stock
        candidates = _ordered(coordinator.db.pending_vision_approved_items(), source)
        for item in candidates:
            if item.state != State.RECEIVED or not item.affiliate_url:
                continue
            if item.original_path.is_file():
                skipped_existing_original += 1
                continue

            # Rows created by earlier lab iterations may contain the old
            # source-prefixed/product-tail path. Keep any existing immutable
            # original untouched; repair only missing paths before a new download.
            canonical_original = coordinator.storage.original(
                item.content_id,
                ".mp4",
                original_url=item.original_url,
                source_id=item.source_id,
            )
            if item.original_path != canonical_original:
                old_path = item.original_path
                if old_path.name and old_path.suffix:
                    stale_partial = old_path.with_suffix(old_path.suffix + ".part")
                    if stale_partial.is_file():
                        stale_partial.unlink()
                coordinator.db.repair_original_path(item.content_id, canonical_original)
                item = coordinator.db.get(item.content_id)

            async def materialize(
                target,
                source_id=item.source_id,
                message_id=item.telegram_message_id,
            ):
                if hasattr(source, "sources"):
                    await source.materialize_candidate_async(source_id, message_id, target)
                else:
                    if str(source.source_id) != str(source_id):
                        raise RuntimeError(f"source-mismatch-for-materialization:{source_id}")
                    await source.materialize_candidate_async(message_id, target)

            ingest = IngestMessage(
                telegram_message_id=item.telegram_message_id,
                source_id=item.source_id,
                topic_id=item.topic_id,
                topic_name=item.topic_name,
                original_url=item.original_url,
                materialize=materialize,
            )
            try:
                await coordinator._ensure_source_connection()
                try:
                    await coordinator.sync.materialize_message_async(ingest)
                finally:
                    await coordinator._release_source_connection()
                current = coordinator.db.get(item.content_id)
                if not current.original_path.is_file():
                    raise FileNotFoundError(f"original ausente após download: {current.original_path}")
                per_source[str(current.source_id)] += 1
                processed.append(item.content_id)
            except Exception as exc:
                errors.append(f"stock:{item.content_id}:{exc}")
                marker = getattr(source, "mark_materialization_failed", None)
                if callable(marker):
                    marker()
                log.error(
                    "[CATCH-UP][STOCK] Falha item=%s: %s",
                    item.content_id,
                    exc,
                )

    outcomes: dict[str, int] = {}
    if stage == "sync":
        outcomes["reserved"] = len(processed)
    elif stage == "vision":
        outcomes["approved"] = int(approved)
        outcomes["waiting_vision"] = int(waiting)
        outcomes["retryable_without_decision"] = int(retryable)
        outcomes["recovered_approved_evidence"] = len(interrupted_approved)
    else:
        outcomes["downloaded"] = len(processed)
        outcomes["skipped_existing_original"] = skipped_existing_original
        remaining = coordinator.db.conn.execute(
            "SELECT COUNT(*) FROM items WHERE state=? "
            "AND affiliate_url IS NOT NULL AND TRIM(affiliate_url)<>'' "
            "AND (original_path IS NULL OR TRIM(original_path)='')",
            (State.RECEIVED.value,),
        ).fetchone()[0]
        outcomes["approved_missing_original_path"] = int(remaining)

    report = {
        "stage": stage,
        "processed": len(processed),
        "per_source": dict(per_source),
        "outcomes": outcomes,
        "errors": errors,
        "historical_scan_exhausted": (
            bool(getattr(source, "historical_scan_exhausted", False))
            if stage == "sync"
            else None
        ),
        "historical_complete": bool(
            source.is_historical_complete()
            if callable(getattr(source, "is_historical_complete", None))
            else coordinator.db.historical_complete()
        ),
    }
    log.info("[CATCH-UP][%s] Relatório: %s", stage.upper(), report)
    return report
