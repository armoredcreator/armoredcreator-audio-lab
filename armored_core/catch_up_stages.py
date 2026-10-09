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
    processed_count = 0
    skipped_existing_original = 0
    approved_this_stage = 0
    waiting_this_stage = 0

    if stage == "sync":
        if bool(getattr(source, "historical_collection_limited", False)):
            raise RuntimeError(
                "ARMORED_SYNC_CATCHUP_LIMIT está configurado com limite finito; "
                "para coleta histórica completa, remova a variável ou configure 0. "
                "Nenhum candidato foi reservado nesta execução."
            )
        iterator_factory = getattr(source, "iter_historical_candidates_async", None)
        if not callable(iterator_factory):
            raise RuntimeError("Sync source não implementa iter_historical_candidates_async()")
        try:
            async for message in iterator_factory():
                source_id = str(getattr(message, "source_id", "") or "")
                message_id = str(message.telegram_message_id)
                try:
                    if source_id not in per_source:
                        raise ValueError(f"unconfigured-source-id:{source_id!r}")
                    if not message_id.isdigit() or int(message_id) <= 0:
                        raise ValueError(f"invalid-telegram-message-id:{message_id!r}")
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
                    processed_count += 1
                except Exception as exc:
                    errors.append(f"sync:{source_id}:{message_id}:{exc}")
                    marker = getattr(source, "mark_materialization_failed", None)
                    if callable(marker):
                        marker()
                    log.error(
                        "[CATCH-UP][SYNC] Falha source=%s mensagem=%s: %s",
                        source_id,
                        message_id,
                        exc,
                    )
                    # Do not reserve later messages after a failed durable write:
                    # preserve source/message ordering and make the incomplete scan
                    # visible to the operator.
                    break
        except Exception as exc:
            errors.append(f"sync:discovery:{type(exc).__name__}:{exc}")
            log.error("[CATCH-UP][SYNC] Descoberta interrompida: %s", exc)
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
        # Repair legacy pre-download RECOVERY rows. Recovery is valid only
        # when an immutable ORIGINAL exists; otherwise retry from the durable
        # Vision decision instead of stranding the candidate.
        pre_download_recovery = coordinator.db.pre_download_recovery_items()
        for item in _ordered(pre_download_recovery, source):
            target_state = (
                State.RECEIVED
                if str(item.affiliate_url or "").strip()
                else State.VISION
            )
            coordinator.db.transition(
                item.content_id,
                target_state,
                "catch-up-repaired-recovery-without-original",
            )

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
                processed_count += 1
                if current.state == State.WAITING_VISION:
                    waiting_this_stage += 1
                elif current.state == State.RECEIVED and current.affiliate_url:
                    approved_this_stage += 1
                if current.state == State.RECOVERY:
                    errors.append(f"vision:{item.content_id}:falha técnica em RECOVERY")
            except Exception as exc:
                errors.append(f"vision:{item.content_id}:{exc}")
                log.error(
                    "[CATCH-UP][VISION] Falha item=%s: %s",
                    item.content_id,
                    exc,
                )
                # Keep strict source/message ordering: retry this candidate
                # before allowing a later candidate to overtake it.
                break
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
            "SELECT COUNT(*) FROM items WHERE state IN (?, ?) "
            "AND (affiliate_url IS NULL OR TRIM(affiliate_url)='')",
            (State.RECEIVED.value, State.VISION.value),
        ).fetchone()[0]
        log.debug(
            "[CATCH-UP][VISION] aprovados=%s aguardando=%s retryable=%s",
            approved,
            waiting,
            retryable,
        )

    else:  # stock
        from .armored_stock import ArmoredStock

        stock_report = await ArmoredStock(coordinator).run()
        processed_count = int(stock_report["processed"])
        per_source.update(stock_report["per_source"])
        skipped_existing_original = int(
            stock_report["outcomes"]["skipped_existing_original"]
        )
        errors.extend(stock_report["errors"])

    outcomes: dict[str, int] = {}
    if stage == "sync":
        outcomes["reserved"] = processed_count
    elif stage == "vision":
        outcomes["approved_this_stage"] = approved_this_stage
        outcomes["waiting_vision_this_stage"] = waiting_this_stage
        outcomes["approved_total"] = int(approved)
        outcomes["waiting_vision_total"] = int(waiting)
        outcomes["retryable_without_decision_total"] = int(retryable)
        outcomes["recovered_pre_download_recovery"] = len(pre_download_recovery)
        outcomes["recovered_approved_evidence"] = len(interrupted_approved)
    else:
        outcomes["downloaded"] = processed_count
        outcomes["skipped_existing_original"] = skipped_existing_original
        outcomes["approved_missing_original"] = int(
            stock_report["outcomes"]["approved_missing_original"]
        )

    report = {
        "stage": stage,
        "processed": processed_count,
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
    log.debug("[CATCH-UP][%s] Relatório: %s", stage.upper(), report)
    return report
