from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class PipelineTrace:
    """Single human-readable + machine-readable trace for one pipeline run.

    Trace is observability only: it never changes pipeline state or controls
    retries, checkpoints, publication, or recovery.
    """

    def __init__(self, root: Path) -> None:
        self.path = Path(root) / "storage" / "logs" / "pipeline_trace.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.log = logging.getLogger("armored.pipeline.trace")

    @staticmethod
    def _clean(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            text = value
        else:
            text = str(value)
        if isinstance(text, str) and len(text) > 500:
            return text[:497] + "..."
        return text

    def emit(self, item_id: str, stage: str, event: str, **details: Any) -> None:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "item_id": str(item_id),
            "stage": str(stage),
            "event": str(event),
        }
        payload.update({key: self._clean(value) for key, value in details.items()})

        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

        detail_text = " ".join(
            f"{key}={value!r}"
            for key, value in payload.items()
            if key not in {"ts", "item_id", "stage", "event"}
        )
        suffix = f" | {detail_text}" if detail_text else ""
        self.log.debug(
            "[TRACE][ITEM %s] %-10s %-14s%s",
            item_id,
            stage,
            event,
            suffix,
        )

    def stage(self, item_id: str, stage: str):
        return _TraceStage(self, str(item_id), str(stage))


class _TraceStage:
    def __init__(self, trace: PipelineTrace, item_id: str, stage: str) -> None:
        self.trace = trace
        self.item_id = item_id
        self.stage = stage
        self.started = 0.0

    def __enter__(self):
        self.started = time.perf_counter()
        self.trace.emit(self.item_id, self.stage, "START")
        return self

    def __exit__(self, exc_type, exc, _tb):
        duration_ms = round((time.perf_counter() - self.started) * 1000, 1)
        if exc is None:
            self.trace.emit(
                self.item_id,
                self.stage,
                "END",
                duration_ms=duration_ms,
            )
        else:
            self.trace.emit(
                self.item_id,
                self.stage,
                "ERROR",
                duration_ms=duration_ms,
                error_type=type(exc).__name__,
                error=str(exc),
            )
        return False
