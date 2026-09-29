import asyncio
import os
import unittest
from pathlib import Path

from ArmoredSync.service import SyncMessage, TelegramSource


class _DB:
    def __init__(self):
        self.checkpoints = {1: 10, 2: 20, 3: 30}

    def sync_topic_checkpoint(self, topic_id):
        return self.checkpoints.get(topic_id, 0)


class _Client:
    def __init__(self):
        self.requested_topics = []

    def iter_messages(self, source, **kwargs):
        topic_id = kwargs["reply_to"]
        self.requested_topics.append(topic_id)
        limit = kwargs.get("limit")
        assert limit == 2

        async def gen():
            if topic_id == 2:
                yield type("Message", (), {
                    "id": 21,
                    "video": True,
                    "message": "https://shopee.com.br/item/21",
                })()
            return

        return gen()


class _Reader:
    def __init__(self):
        self.client = _Client()
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def connect(self):
        self.connect_calls += 1

    async def disconnect(self):
        self.disconnect_calls += 1


class LivePollingRateTests(unittest.TestCase):
    def test_live_polls_one_topic_per_cycle_in_round_robin(self):
        source = TelegramSource.__new__(TelegramSource)
        source.root = Path(".")
        source.reader = _Reader()
        source.db = _DB()
        source._seen = set()
        source._topics = [(1, "one"), (2, "two"), (3, "three")]
        source._live_topic_index = 0

        previous = os.environ.get("ARMORED_SYNC_SOURCE")
        os.environ["ARMORED_SYNC_SOURCE"] = "123456"

        async def scenario():
            first, _ = await source.fetch_live_candidate_async()
            self.assertIsNone(first)
            second, checkpoints = await source.fetch_live_candidate_async()
            self.assertIsInstance(second, SyncMessage)
            self.assertEqual(second.telegram_message_id, "21")
            self.assertEqual(checkpoints, {2: 21})
            self.assertEqual(source.reader.client.requested_topics, [1, 2])

        try:
            asyncio.run(scenario())
        finally:
            if previous is None:
                os.environ.pop("ARMORED_SYNC_SOURCE", None)
            else:
                os.environ["ARMORED_SYNC_SOURCE"] = previous


if __name__ == "__main__":
    unittest.main()
