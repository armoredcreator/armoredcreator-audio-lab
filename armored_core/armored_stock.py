from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from .models import State
from .services import IngestMessage

log = logging.getLogger(__name__)


class ArmoredStock:
    """Independent, one-at-a-time materializer for Vision-approved originals.

    The tool owns only download/materialization. It shares the Coordinator's
    SQLite, Storage, and Telegram Sync adapters; it never runs Vision, Studio,
    IA, Hub, publication, cleanup, or LIVE transitions.
    """

    def __init__(self, coordinator: Any) -> None:
        self.coordinator = coordinator

    def _ordered(self, items: list[Any]) -> list[Any]:
        source = self.coordinator.source
        rank = {
            str(candidate.source_id): index
            for index, candidate in enumerate(getattr(source, "sources", ()))
            if getattr(candidate, "source_id", None) is not None
        }
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

    async def run(self) -> dict[str, Any]:
        """Download eligible originals in configured source order, then stop."""
        coordinator = self.coordinator
        source = coordinator.source
        if source is None:
            raise RuntimeError("ArmoredStock: Sync source não configurado")
        if len(tuple(getattr(source, "sources", ()))) < 3:
            raise RuntimeError("ArmoredStock exige as três fontes configuradas em project.env")

        # Never download while a candidate still needs a Vision decision, or
        # when Vision evidence was committed but its state transition was not.
        pending_vision = coordinator.db.pending_vision_candidates()
        interrupted_approved = coordinator.db.vision_approved_interrupted_items()
        invalid_recovery = coordinator.db.pre_download_recovery_items()
        if pending_vision or interrupted_approved or invalid_recovery:
            raise RuntimeError(
                "ArmoredStock bloqueado: Vision ainda tem candidatos sem decisão "
                "ou evidência interrompida. Execute Vision até terminar sem erro; "
                "nenhum download foi iniciado."
            )

        counts: Counter[str] = Counter(
            {str(candidate.source_id): 0 for candidate in source.sources}
        )
        errors: list[str] = []
        downloaded = 0
        skipped_existing_original = 0
        candidates = self._ordered(coordinator.db.pending_vision_approved_items())

        for item in candidates:
            if item.state != State.RECEIVED or not str(item.affiliate_url or "").strip():
                continue
            if item.original_path.is_file():
                skipped_existing_original += 1
                continue

            # Repair only a missing path. Never overwrite an immutable ORIGINAL.
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
                    raise FileNotFoundError(
                        f"original ausente após download: {current.original_path}"
                    )
                counts[str(current.source_id)] += 1
                downloaded += 1
            except Exception as exc:
                errors.append(f"stock:{item.content_id}:{exc}")
                marker = getattr(source, "mark_materialization_failed", None)
                if callable(marker):
                    marker()
                log.error("[ARMOREDSTOCK] Falha item=%s: %s", item.content_id, exc)
                # Preserve deterministic order: stop on first failed download.
                break

        remaining = sum(
            1
            for candidate in coordinator.db.pending_vision_approved_items()
            if candidate.state == State.RECEIVED
            and str(candidate.affiliate_url or "").strip()
            and not candidate.original_path.is_file()
        )
        return {
            "tool": "ArmoredStock",
            "processed": downloaded,
            "per_source": dict(counts),
            "outcomes": {
                "downloaded": downloaded,
                "skipped_existing_original": skipped_existing_original,
                "approved_missing_original": int(remaining),
            },
            "errors": errors,
        }
