from __future__ import annotations

import logging
from pathlib import Path

from .models import State


class StartupReconciler:
    """Deterministic startup inventory/reconciliation before Sync."""

    def __init__(self, db, storage, publisher=None):
        self.db = db
        self.storage = storage
        self.publisher = publisher
        self.log = logging.getLogger(__name__)

    def run(self) -> dict:
        rows = self.db.conn.execute(
            "SELECT content_id,telegram_message_id,state,source_id,original_path,working_path,result_path,"
            "cleanup_completed FROM items ORDER BY created_at,content_id"
        ).fetchall()
        expected_workspaces: set[Path] = set()
        summary = {
            "items": len(rows), "published": 0, "pending": 0, "failed": 0,
            "clean": 0, "storage_workspaces": 0, "orphans": [],
            "publication_confirmed": 0, "publication_ambiguous": 0,
        }

        self.log.info("[STARTUP][AUDIT] Iniciando varredura antes do Sync")

        for row in rows:
            item_id = str(row["content_id"])
            state = str(row["state"])
            stored_original = str(row["original_path"] or "").strip()
            workspace = (
                Path(stored_original).parent
                if stored_original
                else self.storage.workspace_path(
                    item_id,
                    source_id=str(row["source_id"] or "telegram"),
                )
            )
            workspace = workspace.resolve()
            expected_workspaces.add(workspace)
            files = (
                sorted(p.name for p in workspace.iterdir() if p.is_file())
                if workspace.is_dir()
                else []
            )

            if workspace.is_dir():
                summary["storage_workspaces"] += 1

            publication = self.db.publication(item_id)
            if publication:
                if publication["confirmed"] and publication["published_message_id"]:
                    pub_state = f"CONFIRMED#{publication['published_message_id']}"
                else:
                    publication_check = self._reconcile_publication(item_id)
                    publication = self.db.publication(item_id)
                    if publication and publication["confirmed"] and publication["published_message_id"]:
                        pub_state = f"CONFIRMED#{publication['published_message_id']}"
                    else:
                        pub_state = "AMBIGUOUS"

                if pub_state.startswith("CONFIRMED#"):
                    summary["publication_confirmed"] += 1
                else:
                    summary["publication_ambiguous"] += 1
            else:
                pub_state = "NO_RECORD"
                publication_check = None

            current = self.db.get(item_id)

            # Older releases incorrectly classified Caption failures as
            # WAITING_VISION. Migrate only unmistakable historical Caption
            # evidence to RECOVERY so WAITING_VISION remains Vision-only.
            legacy_error = str(self.db.last_error(item_id) or "")
            legacy_caption = (
                "Nenhuma das " in legacy_error
                and "passou pela Policy" in legacy_error
            ) or "Gemini não conseguiu gerar uma legenda válida" in legacy_error
            if current.state == State.WAITING_VISION and legacy_caption:
                self.db.transition(
                    item_id,
                    State.RECOVERY,
                    "legacy-caption-waiting-vision-migrated-to-recovery",
                )
                current = self.db.get(item_id)

            if (
                current.state == State.FAILED
                and publication is not None
                and not publication["confirmed"]
                and pub_state == "AMBIGUOUS"
                and locals().get("publication_check") == "ABSENT"
            ):
                result = current.result_path
                if not result and current.affiliate_url:
                    result = self.storage.result(
                        current.telegram_message_id,
                        current.affiliate_url,
                        current.affiliate_name,
                        source_id=current.source_id,
                    )
                if result is not None and result.is_file():
                    self.db.transition(
                        item_id,
                        State.RECOVERY,
                        "startup-recover-failed-absent-durable-result",
                    )
                    current = self.db.get(item_id)
                    self.log.info(
                        "[STARTUP][RECOVERY] id=%s FAILED -> RECOVERY "
                        "(publication ABSENT + durable result)",
                        item_id,
                    )

            state = current.state.value
            if state == State.PUBLISHED and bool(current.cleanup_completed):
                summary["published"] += 1
                summary["clean"] += 1
            elif state == State.FAILED.value:
                summary["failed"] += 1
            else:
                summary["pending"] += 1

            self.log.debug(
                "[STARTUP][ITEM] id=%s state=%s publication=%s cleanup=%s files=%s",
                item_id, state, pub_state,
                "OK" if row["cleanup_completed"] else "PENDENTE",
                ",".join(files) if files else "-",
            )

        for video_root in self.storage.video_roots():
            if not video_root.is_dir():
                continue
            for workspace in sorted(video_root.iterdir()):
                if not workspace.is_dir() or workspace.resolve() in expected_workspaces:
                    continue
                if video_root == self.storage.videos:
                    orphan_label = workspace.name
                    location = f"storage/videos/{workspace.name}"
                else:
                    orphan_label = str(workspace.relative_to(self.storage.storage))
                    location = f"storage/{orphan_label}"
                summary["orphans"].append(orphan_label)
                self.log.warning(
                    "[STARTUP][ORPHAN] %s sem registro SQLite",
                    location,
                )

        self.log.info(
            "[STARTUP][AUDIT] concluída: itens=%s publicados=%s pendentes=%s "
            "falhos=%s limpos=%s workspaces=%s órfãos=%s publicações_confirmadas=%s ambíguas=%s",
            summary["items"], summary["published"], summary["pending"],
            summary["failed"], summary["clean"], summary["storage_workspaces"],
            len(summary["orphans"]), summary["publication_confirmed"],
            summary["publication_ambiguous"],
        )
        return summary

    def _reconcile_publication(self, item_id: str) -> str | None:
        if self.publisher is None:
            return None
        item = self.db.get(item_id)
        try:
            result = self.publisher.check_publication(item)
        except Exception as exc:
            self.log.exception(
                "[STARTUP][PUBLICATION] id=%s erro ao verificar: %s",
                item_id, exc,
            )
            return None

        value = getattr(result, "value", str(result))
        if value == "CONFIRMED":
            publication = self.db.publication(item_id)
            message_id = publication["published_message_id"] if publication else None
            if message_id:
                self.db.publication_confirmed(item_id, str(message_id))
                self.log.debug(
                    "[STARTUP][PUBLICATION] id=%s CONFIRMED message_id=%s",
                    item_id, message_id,
                )
        elif value == "ABSENT":
            self.log.debug("[STARTUP][PUBLICATION] id=%s ABSENT", item_id)
        else:
            self.log.warning("[STARTUP][PUBLICATION] id=%s UNKNOWN", item_id)
        return value
    