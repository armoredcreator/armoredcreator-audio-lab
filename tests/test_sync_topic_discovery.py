import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace

from ArmoredSync.service import TelegramSource


class FakeTelegramClient:
    def __init__(self, topic_pages=None, messages=()):
        self.topic_pages = topic_pages or {}
        self.messages = list(messages)
        self.requests = []
        self.iter_messages_args = None

    async def __call__(self, request):
        self.requests.append(request)
        offset_topic = int(getattr(request, "offset_topic", 0) or 0)
        return SimpleNamespace(topics=self.topic_pages.get(offset_topic, []))

    async def iter_messages(self, source, limit=None, reverse=False):
        self.iter_messages_args = (source, limit, reverse)
        for message in self.messages:
            yield message


class TelegramDiscoveryTests(unittest.TestCase):
    def make_source(self, client):
        return TelegramSource(
            Path("."),
            SimpleNamespace(client=client),
            db=None,
            source="-100123",
            source_id="-100123",
        )

    def test_regular_chat_falls_back_to_general_history(self):
        client = FakeTelegramClient(topic_pages={0: []})
        source = self.make_source(client)

        topics = asyncio.run(source._discover_topics("-100123"))

        self.assertEqual(topics, [(0, "Geral")])

    def test_forum_topic_discovery_paginates_past_first_100(self):
        first_page = [
            SimpleNamespace(id=i, title=f"topic-{i}", top_message=i * 10, date=None)
            for i in range(1, 101)
        ]
        second_page = [
            SimpleNamespace(id=i, title=f"topic-{i}", top_message=i * 10, date=None)
            for i in range(101, 103)
        ]
        client = FakeTelegramClient(topic_pages={0: first_page, 100: second_page})
        source = self.make_source(client)

        topics = asyncio.run(source._discover_topics("-100123"))

        self.assertEqual(len(topics), 102)
        self.assertEqual(topics[0], (1, "topic-1"))
        self.assertEqual(topics[-1], (102, "topic-102"))
        self.assertEqual(len(client.requests), 2)

    def test_forum_video_matches_product_link_posted_immediately_before_it(self):
        product_url = "https://shopee.com.br/product/12"
        messages = [
            SimpleNamespace(id=102, video=None, message=product_url, entities=[], grouped_id=None),
            SimpleNamespace(id=101, video=object(), message="", entities=[], grouped_id=None),
            SimpleNamespace(id=100, video=None, message="older unrelated message", entities=[], grouped_id=None),
        ]

        class CandidateSource(TelegramSource):
            async def _topic_messages(self, _source, _topic_id):
                for message in messages:
                    yield message

        source = CandidateSource(
            Path("."),
            SimpleNamespace(),
            db=None,
            source="-100123",
            source_id="-100123",
        )

        async def collect():
            return [
                candidate
                async for candidate in source._candidate_iterator(
                    -100123,
                    [(500, "topic")],
                )
            ]

        candidates = asyncio.run(collect())

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0][0], 101)
        self.assertEqual(candidates[0][4], product_url)

    def test_general_history_video_matches_product_link_posted_after_it(self):
        product_url = "https://shopee.com.br/product/13"
        messages = [
            SimpleNamespace(id=101, video=object(), message="", entities=[], grouped_id=None),
            SimpleNamespace(id=102, video=None, message=product_url, entities=[], grouped_id=None),
        ]

        class CandidateSource(TelegramSource):
            async def _topic_messages(self, _source, _topic_id):
                for message in messages:
                    yield message

        source = CandidateSource(
            Path("."),
            SimpleNamespace(),
            db=None,
            source="-100123",
            source_id="-100123",
        )

        async def collect():
            return [
                candidate
                async for candidate in source._candidate_iterator(
                    -100123,
                    [(500, "topic")],
                )
            ]

        candidates = asyncio.run(collect())

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0][0], 101)
        self.assertEqual(candidates[0][4], product_url)

    def test_general_history_is_streamed_without_loading_media(self):
        messages = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        client = FakeTelegramClient(messages=messages)
        source = self.make_source(client)

        async def collect():
            return [message async for message in source._topic_messages("-100123", 0)]

        actual = asyncio.run(collect())

        self.assertEqual(actual, messages)
        self.assertEqual(client.iter_messages_args, ("-100123", None, True))


if __name__ == "__main__":
    unittest.main()
