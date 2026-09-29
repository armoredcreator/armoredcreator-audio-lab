from __future__ import annotations
from pathlib import Path
import logging
import shutil
from .database import Database
from .models import PublicationCheck, State
from .services import Publisher, StudioService, VisionService, VisionUnresolvedError, PublicationUnknownError
from .storage import Storage
from .trace import PipelineTrace


class Pipeline:
    def __init__(self, db: Database, storage: Storage, vision: VisionService, studio: StudioService, publisher: Publisher):
        self.db, self.storage = db, storage
        self.vision, self.studio, self.publisher = vision, studio, publisher
        self.log = logging.getLogger(__name__)
        self.trace = PipelineTrace(storage.root)
        self._shutdown_checker = lambda: False

    def set_shutdown_checker(self, checker) -> None:
        """Attach the Coordinator shutdown signal without coupling layers."""
        self._shutdown_checker = checker

    def run(self, item_id: str) -> None:
        item = self.db.get(item_id)
        self.trace.emit(item_id, "PIPELINE", "START", state=item.state.value, attempt=item.attempts + 1)
        self.log.info("[PIPELINE][ITEM %s] início state=%s", item.content_id, item.state.value)
        if item.state == State.PUBLISHED:
            if not item.cleanup_completed:
                self.cleanup(item_id)
            self.trace.emit(item_id, "PIPELINE", "END", state=State.PUBLISHED.value, cleanup=item.cleanup_completed)
            return
        try:
            self.db.record_attempt(item_id)
            if not item.original_path.is_file():
                raise FileNotFoundError(f"immutable-original-missing: {item.original_path}")
            if item.state == State.RECEIVED:
                self.db.transition(item_id, State.VISION, "pipeline-start")
                self.trace.emit(item_id, "PIPELINE", "TRANSITION", old_state=State.RECEIVED.value, new_state=State.VISION.value, reason="pipeline-start")
            elif item.state == State.RECOVERY:
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
                    self.db.transition(item_id, State.PUBLISHING, "recovery-resume-publication")
                    self.trace.emit(item_id, "RECOVERY", "TRANSITION", old_state=State.RECOVERY.value, new_state=State.PUBLISHING.value, reason="recovery-resume-publication")
                elif item.working_path and item.working_path.is_file() and item.affiliate_name:
                    self.db.transition(item_id, State.STUDIO, "recovery-resume-studio")
                    self.trace.emit(item_id, "RECOVERY", "TRANSITION", old_state=State.RECOVERY.value, new_state=State.STUDIO.value, reason="recovery-resume-studio")
                elif item.affiliate_name:
                    self.db.transition(item_id, State.STUDIO, "recovery-rebuild-working")
                    self.trace.emit(item_id, "RECOVERY", "TRANSITION", old_state=State.RECOVERY.value, new_state=State.STUDIO.value, reason="recovery-rebuild-working")
                else:
                    self.db.transition(item_id, State.VISION, "recovery-rebuild-vision")
                    self.trace.emit(item_id, "RECOVERY", "TRANSITION", old_state=State.RECOVERY.value, new_state=State.VISION.value, reason="recovery-rebuild-vision")
            item = self.db.get(item_id)
            if item.state == State.WAITING_VISION:
                self.trace.emit(item_id, "VISION", "BLOCKED", state=State.WAITING_VISION.value, reason=self.db.last_error(item_id) or "vision-waiting")
                return
            if item.state == State.FAILED:
                raise RuntimeError("FAILED item requires deterministic recovery before pipeline.run")

            if item.state == State.VISION:
                self.log.info("[PIPELINE][ITEM %s] VISION iniciando", item.content_id)
                with self.trace.stage(item_id, "VISION"):
                    try:
                        v = self.vision.identify(item)
                    except VisionUnresolvedError as exc:
                        self.db.mark_vision_waiting(item_id, str(exc))
                        self.trace.emit(item_id, "VISION", "WAITING", reason=str(exc))
                        return
                    self.db.set_vision(
                        item_id,
                        v.affiliate_name,
                        v.affiliate_url,
                        affiliate_urls=getattr(v, "affiliate_urls", ()),
                        publication_caption=getattr(v, "publication_caption", None),
                    )
                    self.trace.emit(
                        item_id,
                        "VISION",
                        "RESULT",
                        affiliate_links=len(getattr(v, "affiliate_urls", ()) or ()),
                        caption=bool(getattr(v, "publication_caption", None)),
                    )
                self.log.info("[PIPELINE][ITEM %s] VISION concluída", item_id)
                self.db.transition(item_id, State.STUDIO, "vision-complete")
                self.trace.emit(item_id, "VISION", "TRANSITION", old_state=State.VISION.value, new_state=State.STUDIO.value, reason="vision-complete")

            item = self.db.get(item_id)
            if item.state == State.STUDIO:
                self.log.info("[PIPELINE][ITEM %s] STUDIO/RVC iniciando", item.content_id)
                if not item.affiliate_name:
                    raise RuntimeError("studio-requires-affiliate-metadata")
                with self.trace.stage(item_id, "STUDIO"):
                    studio = self.studio.process(item)
                    if studio.working_path is not None:
                        if not studio.working_path.is_file():
                            raise FileNotFoundError("studio-working-file-missing")
                        self.db.set_working(item_id, studio.working_path)
                    if not studio.result_path.is_file():
                        raise FileNotFoundError("studio-result-file-missing")
                    self.db.set_result(item_id, studio.result_path)
                    self.trace.emit(
                        item_id,
                        "STUDIO",
                        "RESULT",
                        working=bool(studio.working_path),
                        result_exists=studio.result_path.is_file(),
                    )
                self.log.info("[PIPELINE][ITEM %s] STUDIO/RVC concluído result=%s", item_id, studio.result_path)
                self.db.transition(item_id, State.PUBLISHING, "studio-complete")
                self.trace.emit(item_id, "STUDIO", "TRANSITION", old_state=State.STUDIO.value, new_state=State.PUBLISHING.value, reason="studio-complete")

            item = self.db.get(item_id)
            if item.state == State.PUBLISHING:
                self.log.info("[PIPELINE][ITEM %s] HUB/PUBLICAÇÃO iniciando", item.content_id)
                if not item.result_path or not item.result_path.is_file():
                    raise FileNotFoundError("publication-result-missing")

                with self.trace.stage(item_id, "HUB"):
                    publish_once = getattr(self.publisher, "publish_once", None)
                    if callable(publish_once):
                        try:
                            result = publish_once(item)
                        except PublicationUnknownError as exc:
                            self.db.transition(item_id, State.RECOVERY, str(exc))
                            self.trace.emit(item_id, "HUB", "UNKNOWN", reason=str(exc), new_state=State.RECOVERY.value)
                            return

                        if not result.confirmed:
                            raise RuntimeError("publication-not-confirmed")
                        if not result.message_id:
                            raise RuntimeError("confirmed-publication-without-message-id")
                        self.db.publication_confirmed(item_id, str(result.message_id))
                        self.trace.emit(item_id, "TELEGRAM", "CONFIRMED", message_id=str(result.message_id))
                    else:
                        self.db.publication_started(item_id)
                        check = self.publisher.check_publication(item)
                        self.trace.emit(item_id, "HUB", "CHECK", result=check.value)
                        if check == PublicationCheck.UNKNOWN:
                            self.db.transition(
                                item_id,
                                State.RECOVERY,
                                "publication-check-uncertain-refusing-to-publish",
                            )
                            self.trace.emit(item_id, "TELEGRAM", "UNKNOWN", reason="publication-check-uncertain-refusing-to-publish", new_state=State.RECOVERY.value)
                            return

                        if check == PublicationCheck.ABSENT:
                            try:
                                result = self.publisher.publish(item)
                            except PublicationUnknownError as exc:
                                self.db.transition(item_id, State.RECOVERY, str(exc))
                                self.trace.emit(item_id, "TELEGRAM", "UNKNOWN", reason=str(exc), new_state=State.RECOVERY.value)
                                return

                            if not result.confirmed:
                                raise RuntimeError("publication-not-confirmed")
                            if not result.message_id:
                                raise RuntimeError("confirmed-publication-without-message-id")
                            self.db.publication_confirmed(item_id, str(result.message_id))
                            self.trace.emit(item_id, "TELEGRAM", "CONFIRMED", message_id=str(result.message_id))
                        else:
                            pub = self.db.publication(item_id)
                            message_id = pub["published_message_id"] if pub else None
                            if not message_id:
                                self.db.transition(
                                    item_id,
                                    State.RECOVERY,
                                    "confirmed-publication-without-real-message-id",
                                )
                                self.trace.emit(item_id, "TELEGRAM", "UNKNOWN", reason="confirmed-publication-without-real-message-id", new_state=State.RECOVERY.value)
                                return
                            self.db.publication_confirmed(item_id, str(message_id))
                            self.trace.emit(item_id, "TELEGRAM", "CONFIRMED", message_id=str(message_id))

                self.db.transition(item_id, State.PUBLISHED, "publication-confirmed")
                self.trace.emit(item_id, "PIPELINE", "TRANSITION", old_state=State.PUBLISHING.value, new_state=State.PUBLISHED.value, reason="publication-confirmed")
                self.log.info("[PIPELINE][ITEM %s] PUBLICADO confirmado; cleanup iniciando", item_id)
                self.cleanup(item_id)
                self.log.info("[PIPELINE][ITEM %s] FINALIZADO PUBLISHED+cleanup", item_id)
                self.trace.emit(item_id, "PIPELINE", "END", state=State.PUBLISHED.value, cleanup=True)
        except Exception as exc:
            current = self.db.get(item_id)
            if current.state == State.PUBLISHED:
                self.trace.emit(item_id, "PIPELINE", "ERROR_AFTER_PUBLISH", error_type=type(exc).__name__, error=str(exc))
                raise
            if self._shutdown_checker():
                self.log.warning(
                    "[PIPELINE][ITEM %s] shutdown solicitado; preservando state=%s para recovery: %s",
                    item_id, current.state.value, exc,
                )
                self.trace.emit(item_id, "PIPELINE", "SHUTDOWN", state=current.state.value, error_type=type(exc).__name__, error=str(exc))
                raise KeyboardInterrupt from exc
            reason = f"{type(exc).__name__}: {exc}"
            # Processing-stage failures are recoverable by default. The item
            # remains the single authoritative candidate and the Coordinator
            # must reconcile/resume it from the persisted artifacts/state on
            # the next recovery pass. Generic exceptions must never silently
            # abandon the current candidate.
            self.db.transition(item_id, State.RECOVERY, reason)
            self.log.error(
                "[PIPELINE][ITEM %s] ERRO RECOVERABLE state=%s: %s",
                item_id,
                current.state.value,
                exc,
            )
            self.trace.emit(
                item_id,
                "PIPELINE",
                "RECOVERY",
                state=current.state.value,
                new_state=State.RECOVERY.value,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise

    def cleanup(self, item_id: int) -> None:
        item = self.db.get(item_id)
        if item.state != State.PUBLISHED:
            raise RuntimeError("cleanup-is-allowed-only-after-PUBLISHED")
        workspace = item.workspace.resolve()
        original = item.original_path.resolve()
        if workspace != original.parent.resolve():
            raise RuntimeError("cleanup-workspace-mismatch")
        with self.trace.stage(item_id, "CLEANUP"):
            if not workspace.is_dir():
                self.db.mark_cleanup_completed(item_id)
                return
            for path in workspace.iterdir():
                resolved = path.resolve()
                if resolved == original:
                    continue
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            self.db.mark_cleanup_completed(item_id)
