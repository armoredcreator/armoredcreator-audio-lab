from __future__ import annotations

import shutil

from .database import Database
from .models import PublicationCheck, State
from .pipeline import Pipeline
from .services import Publisher, StudioService, VisionService
from .storage import Storage


class Recovery:
    """Evidence-driven recovery for one durable pipeline item."""

    def __init__(
        self,
        db: Database,
        storage: Storage,
        vision: VisionService,
        studio: StudioService,
        publisher: Publisher,
    ):
        self.db, self.storage = db, storage
        self.pipeline = Pipeline(db, storage, vision, studio, publisher)

    def _reset_processing_artifacts(self, item_id: str) -> None:
        """Keep only the immutable ORIGINAL before rebuilding derived artifacts."""
        item = self.db.get(item_id)
        original = item.original_path.resolve()
        workspace = item.workspace.resolve()

        if workspace != original.parent.resolve():
            raise RuntimeError("recovery-workspace-mismatch")

        if workspace.is_dir():
            for path in list(workspace.iterdir()):
                if path.resolve() == original:
                    continue
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()

        self.db.set_working(item_id, None)
        self.db.set_result(item_id, None)

    def _rebuild_processing_from_original(self, item_id: str) -> None:
        self._reset_processing_artifacts(item_id)
        self.db.transition(item_id, State.STUDIO, "recovery-rebuild-studio-from-original")
        self.pipeline.run(item_id)

    def _resume_durable_result(self, item_id: str, reason: str) -> None:
        self.db.transition(item_id, State.PUBLISHING, reason)
        self.pipeline.run(item_id)

    def reconcile(self, item_id: str) -> None:
        item = self.db.get(item_id)

        # ShopeeProductNotFound/VisionUnresolved remains WAITING_VISION.
        if item.state == State.WAITING_VISION:
            self.db.transition(item_id, State.VISION, "recovery-retry-waiting-vision")
            item = self.db.get(item_id)

        if item.state == State.PUBLISHED:
            self.pipeline.cleanup(item_id)
            return

        if not item.original_path.is_file():
            raise FileNotFoundError(
                f"cannot-recover-without-immutable-original: {item.original_path}"
            )

        self.db.record_recovery(item_id)
        self.db.transition(item_id, State.RECOVERY, "startup-recovery")
        item = self.db.get(item_id)

        # Telegram is a separate side-effect boundary. Reconcile it before
        # changing processing artifacts whenever a publication attempt exists.
        pub = self.db.publication(item_id)
        if pub:
            if pub["confirmed"]:
                message_id = pub["published_message_id"]
                if not message_id:
                    raise RuntimeError("confirmed-publication-without-real-message-id")
                self.db.transition(item_id, State.PUBLISHED, "db-publication-already-confirmed")
                self.pipeline.cleanup(item_id)
                return

            check = self.pipeline.publisher.check_publication(item)
            if check == PublicationCheck.UNKNOWN:
                raise RuntimeError("publication-check-uncertain-recovery-stopped")

            if check == PublicationCheck.CONFIRMED:
                refreshed = self.db.publication(item_id)
                message_id = refreshed["published_message_id"] if refreshed else None
                if not message_id:
                    raise RuntimeError("telegram-confirmed-without-real-message-id")
                self.db.publication_confirmed(item_id, str(message_id))
                self.db.transition(item_id, State.PUBLISHED, "publisher-confirms-existing")
                self.pipeline.cleanup(item_id)
                return

            # ABSENT: reuse a proven durable result if one still exists.
            result = item.result_path
            if result and result.is_file():
                self._resume_durable_result(
                    item_id,
                    "publication-absent-reuse-durable-result",
                )
                return

            # No durable result remains: rebuild derived processing safely.
            if item.affiliate_name:
                self._rebuild_processing_from_original(item_id)
                return

            self.db.transition(item_id, State.VISION, "publication-absent-rebuild-vision")
            self.pipeline.run(item_id)
            return

        # No publication attempt exists. A real durable result is already a
        # safe publication boundary; do not destroy it just because the item
        # is in RECOVERY/FAILED.
        result = item.result_path
        if not result and item.affiliate_url:
            result = self.storage.result(
                item_id,
                item.affiliate_url,
                item.affiliate_name,
            )
        if result and result.is_file():
            if item.result_path is None:
                self.db.set_result(item_id, result)
            self._resume_durable_result(item_id, "recovery-resume-durable-result")
            return

        # No proven final result. Any working/derived artifacts may be partial,
        # so discard them and rebuild from the immutable ORIGINAL.
        if item.affiliate_name:
            self._rebuild_processing_from_original(item_id)
            return

        # Without resolved product metadata, Vision is the only safe boundary.
        self.db.transition(item_id, State.VISION, "recovery-rebuild-vision-from-original")
        self.pipeline.run(item_id)
