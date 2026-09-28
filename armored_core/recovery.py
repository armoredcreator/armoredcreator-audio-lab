from __future__ import annotations

import shutil

from .database import Database
from .models import PublicationCheck, State
from .pipeline import Pipeline
from .services import Publisher, StudioService, VisionService
from .storage import Storage


class Recovery:
    """Evidence-driven recovery for one durable pipeline item.

    Recovery never assumes that the last persisted stage is a valid artifact.
    For processing failures it reconstructs the video from the last safe,
    immutable boundary instead of resuming potentially partial Studio/RVC files.

    Telegram publication ambiguity is deliberately handled separately: when a
    publication attempt already exists, Recovery reconciles Telegram first and
    never rebuilds or republishes while the external outcome is UNKNOWN.
    """

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
        """Remove every derived workspace artifact and force a fresh Studio/RVC run.

        The immutable ORIGINAL is the only artifact trusted after a processing
        failure. This intentionally avoids resuming a partially written
        working/result file whose existence alone does not prove correctness.
        """
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

    def reconcile(self, item_id: str) -> None:
        item = self.db.get(item_id)

        # WAITING_VISION remains the existing Vision recovery contract. In
        # particular, ShopeeProductNotFound is represented by Vision V1 as
        # VisionUnresolvedError and remains WAITING_VISION instead of becoming
        # a generic processing failure.
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

        # Publication is a separate external side-effect boundary. Once a
        # publication attempt exists, reconcile Telegram before touching video
        # artifacts. UNKNOWN is a hard safety stop; ABSENT may reuse a valid
        # durable result and publish exactly once through the normal pipeline.
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

            # ABSENT means the publication boundary is unresolved no longer.
            # Keep the durable result and let Pipeline perform the controlled
            # publication. No Vision/Studio rebuild is necessary.
            result = item.result_path
            if result and result.is_file():
                self.db.transition(item_id, State.PUBLISHING, "publication-absent-reuse-durable-result")
                self.pipeline.run(item_id)
                return

            # The publication attempt exists but its result disappeared. The
            # video must be rebuilt before a new publication attempt.
            if item.affiliate_name:
                self._rebuild_processing_from_original(item_id)
                return

            self.db.transition(item_id, State.VISION, "publication-absent-rebuild-vision")
            self.pipeline.run(item_id)
            return

        # No publication attempt exists. A processing-stage failure must not
        # resume from the mere existence of working/result files: they may be
        # partial artifacts from a crashed Studio/RVC process. Rebuild the
        # complete derived video from the immutable ORIGINAL.
        if item.affiliate_name:
            self._rebuild_processing_from_original(item_id)
            return

        # No resolved product metadata means the only safe reconstruction point
        # is Vision. This covers technical Vision failures persisted as RECOVERY.
        self.db.transition(item_id, State.VISION, "recovery-rebuild-vision-from-original")
        self.pipeline.run(item_id)
