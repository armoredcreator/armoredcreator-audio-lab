import json
import tempfile
import unittest
from pathlib import Path

from armored_core.database import Database
from armored_core.models import State
from armored_core.pipeline import Pipeline
from armored_core.services import VisionUnresolvedError
from armored_core.storage import Storage


class TraceVision:
    def identify(self, item):
        raise VisionUnresolvedError("simulated-vision-unresolved")


class TraceStudio:
    def process(self, item):
        raise AssertionError("Studio must not run after unresolved Vision")


class TracePublisher:
    def check_publication(self, item):
        raise AssertionError("Hub must not run after unresolved Vision")


class PipelineTraceTests(unittest.TestCase):
    def test_trace_explains_vision_block_without_changing_state_machine(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            storage = Storage(root)
            db = Database(storage.database / "armoredcreator.db")
            original = storage.workspace("trace-1") / "trace-1_finallinkoriginal.mp4"
            original.parent.mkdir(parents=True, exist_ok=True)
            original.write_bytes(b"trace")

            item_id = db.create_item(
                "trace-1",
                original,
                original_url="https://example.invalid/product",
            )

            pipeline = Pipeline(
                db,
                storage,
                TraceVision(),
                TraceStudio(),
                TracePublisher(),
            )
            try:
                pipeline.run(item_id)

                row = db.get(item_id)
                self.assertEqual(row.state, State.WAITING_VISION)
                pipeline.run(item_id)
                blocked_row = db.get(item_id)
                self.assertEqual(blocked_row.state, State.WAITING_VISION)

                trace_path = root / "storage" / "logs" / "pipeline_trace.jsonl"
                self.assertTrue(trace_path.exists())

                events = [
                    json.loads(line)
                    for line in trace_path.read_text(encoding="utf-8").splitlines()
                ]
                self.assertTrue(events)
                self.assertEqual(events[0]["item_id"], item_id)
                self.assertEqual(events[0]["event"], "START")

                vision_events = [
                    event["event"]
                    for event in events
                    if event["stage"] == "VISION"
                ]
                self.assertIn("START", vision_events)
                self.assertIn("WAITING", vision_events)

                waiting = next(
                    event
                    for event in events
                    if event["stage"] == "VISION" and event["event"] == "WAITING"
                )
                waiting_events = [
                    event for event in events
                    if event["stage"] == "VISION" and event["event"] == "WAITING"
                ]
                self.assertEqual(waiting_events[-1]["reason"], "simulated-vision-unresolved")
                blocked = next(
                    event for event in events
                    if event["stage"] == "VISION" and event["event"] == "BLOCKED"
                )
                self.assertEqual(blocked["reason"], "simulated-vision-unresolved")
            finally:
                db.close()



if __name__ == "__main__":
    unittest.main()
