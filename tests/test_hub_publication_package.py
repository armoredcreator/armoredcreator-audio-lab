from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from armored_core.database import Database

from ArmoredHub.service import ArmoredHub
from armored_core.models import Item, State
from armored_core.services import PublicationUnknownError


def make_item(**kwargs):
    base = dict(
        content_id="1",
        telegram_message_id="1",
        state=State.PUBLISHING,
        workspace=Path("."),
        original_path=Path("original.mp4"),
        working_path=None,
        result_path=Path("result.mp4"),
        affiliate_name="Produto",
        affiliate_url="https://s.shopee.com.br/original",
    )
    base.update(kwargs)
    return Item(**base)


class HubPublicationPackageTests(unittest.TestCase):
    def test_formats_caption_plus_all_links(self):
        item = make_item(
            publication_caption="Olha esse charme ✨\n#casa #decoracao",
            affiliate_urls=(
                "https://s.shopee.com.br/o",
                "https://s.shopee.com.br/1",
                "https://s.shopee.com.br/2",
            ),
        )
        package = ArmoredHub._publication_text(item)
        self.assertEqual(
            package,
            "Olha esse charme ✨\n#casa #decoracao\n\n"
            "https://s.shopee.com.br/o\n"
            "https://s.shopee.com.br/1\n"
            "https://s.shopee.com.br/2",
        )

    def test_keeps_legacy_link_only_publications_compatible(self):
        item = make_item()
        self.assertEqual(
            ArmoredHub._publication_text(item),
            "https://s.shopee.com.br/original",
        )

    def test_telegram_send_timeout_logs_real_error_and_reconciliation(self):
        from unittest.mock import patch

        from telegram.error import TimedOut
        from armored_core.models import PublicationCheck

        with TemporaryDirectory() as td:
            db = Database(Path(td) / "armoredcreator.db")
            try:
                output = Path(td) / "result.mp4"
                output.write_bytes(b"test-video")
                item = make_item(result_path=output)
                hub = ArmoredHub(Path(td), db)

                def fail_run_async(coro):
                    coro.close()
                    raise TimedOut("Telegram read timeout after 60 seconds")

                with patch.dict(
                    "os.environ",
                    {
                        "ARMORED_CREATOR_BOT_TOKEN": "test-token",
                        "ARMORED_HUB_TOPIC_ID": "228",
                    },
                    clear=False,
                ), patch.object(hub, "_resolve_destination_chat_id", return_value="-100123"), patch.object(
                    hub, "_video_metadata", return_value=(720, 1280, 10)
                ), patch.object(
                    hub, "_run_async",
                    side_effect=fail_run_async,
                ), patch.object(
                    hub, "check_publication", return_value=PublicationCheck.ABSENT
                ), patch(
                    "builtins.print"
                ) as mocked_print:
                    with self.assertRaises(PublicationUnknownError):
                        hub._publish_telegram(item, output)

                messages = [
                    call.args[0]
                    for call in mocked_print.call_args_list
                    if call.args
                    and "[HUB][TELEGRAM][SEND_ERROR]" in str(call.args[0])
                ]
                self.assertEqual(len(messages), 1)
                self.assertIn("exception=TimedOut", messages[0])
                self.assertIn("Telegram read timeout after 60 seconds", messages[0])
                self.assertIn("reconciliation=ABSENT", messages[0])
                self.assertIn("decision=UNKNOWN", messages[0])
            finally:
                db.close()

    def test_sent_unverified_zero_matches_is_unknown(self):
        with TemporaryDirectory() as td:
            db = Database(Path(td) / "armoredcreator.db")
            try:
                item = make_item()
                db.publication_started(item.item_id)
                db.conn.execute(
                    "UPDATE publications SET verification_status='SENT_UNVERIFIED' WHERE content_id=?",
                    (item.item_id,),
                )
                db.conn.commit()

                hub = ArmoredHub(Path(td), db)
                hub._find_telegram_publications = lambda _item: []
                self.assertEqual(
                    hub.check_publication(item),
                    __import__("armored_core.models", fromlist=["PublicationCheck"]).PublicationCheck.UNKNOWN,
                )
            finally:
                db.close()

    def test_absent_after_ambiguous_send_restarts_publication(self):
        with TemporaryDirectory() as td:
            db = Database(Path(td) / "armoredcreator.db")
            try:
                item = make_item()
                result_path = Path(td) / "result.mp4"
                result_path.write_bytes(b"test-video")
                item = make_item(result_path=result_path)
                hub = ArmoredHub(Path(td), db)
                calls = []

                def fake_publish(_item, _output):
                    calls.append("send")
                    db.publication_send_started(item.item_id)
                    raise PublicationUnknownError("simulated ambiguous Telegram send")

                hub._publish_telegram = fake_publish
                hub._find_telegram_publications = lambda _item: []

                with self.assertRaises(PublicationUnknownError):
                    hub.publish_once(item)

                with self.assertRaises(PublicationUnknownError):
                    hub.publish_once(item)

                # An ambiguous external send remains UNKNOWN when Telegram
                # provides no exact evidence. Recovery must not republish.
                self.assertEqual(calls, ["send"])
                record = db.publication(item.item_id)
                self.assertIsNotNone(record)
                self.assertEqual(record["verification_status"], "SENT_UNVERIFIED")
            finally:
                db.close()

    def test_new_publication_zero_matches_remains_absent(self):
        with TemporaryDirectory() as td:
            db = Database(Path(td) / "armoredcreator.db")
            try:
                item = make_item()
                db.publication_started(item.item_id)
                hub = ArmoredHub(Path(td), db)
                hub._find_telegram_publications = lambda _item: []
                self.assertEqual(
                    hub.check_publication(item),
                    __import__("armored_core.models", fromlist=["PublicationCheck"]).PublicationCheck.ABSENT,
                )
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
