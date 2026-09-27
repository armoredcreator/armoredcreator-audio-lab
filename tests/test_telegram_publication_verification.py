import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ArmoredHub.service import ArmoredHub
from armored_core.database import Database
from armored_core.storage import Storage


class TelegramPublicationIdentityTests(unittest.TestCase):
    def _item(self, root: Path):
        storage = Storage(root)
        db = Database(storage.database / "db.sqlite")
        item_id = db.create_item(
            "tg-1",
            storage.original("tg-1"),
            original_url="https://shopee.example/a/b/123",
        )
        original = storage.original(item_id)
        original.write_bytes(b"SOURCE")
        db.conn.execute(
            "UPDATE items SET affiliate_name=?, affiliate_url=? WHERE content_id=?",
            ("product", "https://s.shopee.com.br/TEST123", item_id),
        )
        db.conn.commit()
        result = storage.result(item_id, "https://s.shopee.com.br/TEST123", "product")
        result.write_bytes(b"RESULT")
        db.set_result(item_id, result)
        return db, db.get(item_id)

    @staticmethod
    def _message(topic=228, caption="https://s.shopee.com.br/TEST123", video=True):
        return SimpleNamespace(
            id=777,
            message=caption,
            video=SimpleNamespace() if video else None,
            document=None,
            reply_to=SimpleNamespace(
                reply_to_top_id=topic,
                reply_to_msg_id=topic,
            ),
        )

    def test_exact_caption_topic_and_video_are_required(self):
        with tempfile.TemporaryDirectory() as td:
            db, item = self._item(Path(td))
            hub = ArmoredHub(Path(td), db)
            self.assertTrue(hub._telegram_publication_matches(self._message(), item, 228))
            self.assertFalse(
                hub._telegram_publication_matches(self._message(topic=227), item, 228)
            )
            self.assertFalse(
                hub._telegram_publication_matches(
                    self._message(caption="https://s.shopee.com.br/OTHER"),
                    item,
                    228,
                )
            )
            self.assertFalse(
                hub._telegram_publication_matches(self._message(video=False), item, 228)
            )

            # Telegram may expose a different filename (or omit it) after upload;
            # that metadata must not invalidate an exact publication match.
            message = self._message()
            message.document = SimpleNamespace(
                attributes=[SimpleNamespace(file_name="telegram-renamed.mp4")]
            )
            self.assertTrue(hub._telegram_publication_matches(message, item, 228))
            db.close()


    def test_full_caption_and_all_links_are_required_for_new_package(self):
        with tempfile.TemporaryDirectory() as td:
            db, item = self._item(Path(td))
            db.set_vision(
                item.item_id,
                "product",
                "https://s.shopee.com.br/TEST123",
                affiliate_urls=(
                    "https://s.shopee.com.br/TEST123",
                    "https://s.shopee.com.br/CAND2",
                ),
                publication_caption="Olha esse charme ✨\n#casa #decoracao",
            )
            item = db.get(item.item_id)
            hub = ArmoredHub(Path(td), db)
            expected = hub._publication_text(item)
            self.assertTrue(
                hub._telegram_publication_matches(
                    self._message(caption=expected),
                    item,
                    228,
                )
            )
            self.assertFalse(
                hub._telegram_publication_matches(
                    self._message(caption="Olha esse charme ✨\n#casa\n\nhttps://s.shopee.com.br/TEST123"),
                    item,
                    228,
                )
            )
            self.assertFalse(
                hub._telegram_publication_matches(
                    self._message(
                        caption=expected.replace(
                            "https://s.shopee.com.br/CAND2",
                            "https://s.shopee.com.br/OTHER",
                        )
                    ),
                    item,
                    228,
                )
            )
            db.close()

    def test_topic_scoped_match_accepts_missing_reply_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            db, item = self._item(Path(td))
            hub = ArmoredHub(Path(td), db)
            message = self._message()
            message.reply_to = None
            self.assertTrue(
                hub._telegram_publication_matches(
                    message, item, 228, topic_scoped=True
                )
            )
            self.assertFalse(
                hub._telegram_publication_matches(
                    message, item, 228, topic_scoped=False
                )
            )
            db.close()

    def test_message_id_is_stored_unconfirmed(self):
        with tempfile.TemporaryDirectory() as td:
            db, item = self._item(Path(td))
            db.publication_started(
                item.item_id,
                destination_chat_id="-100123",
                destination_topic_id=228,
            )
            db.publication_message_sent(item.item_id, "777")
            row = db.publication(item.item_id)
            self.assertEqual(row["published_message_id"], "777")
            self.assertEqual(row["confirmed"], 0)
            self.assertEqual(row["verification_status"], "SENT_UNVERIFIED")
            db.close()


if __name__ == "__main__":
    unittest.main()
