from __future__ import annotations

import sys
import types
from pathlib import Path

from armored_core.models import Item, PublicationCheck, State
from armored_core.services import PublicationResult
from ArmoredHub.service import ArmoredHub


def _item(result_path: Path) -> Item:
    return Item(
        content_id="550",
        telegram_message_id="550",
        state=State.PUBLISHING,
        workspace=result_path.parent,
        original_path=result_path,
        working_path=result_path,
        result_path=result_path,
        affiliate_name="test",
        affiliate_url="https://s.shopee.com.br/test",
    )


def test_video_metadata_reads_real_geometry_and_duration(monkeypatch, tmp_path):
    class Capture:
        def __init__(self, path):
            self.path = path
            self.released = False

        def isOpened(self):
            return True

        def get(self, prop):
            values = {
                3: 1080.0,
                4: 1920.0,
                7: 240.0,
                5: 24.0,
            }
            return values[prop]

        def release(self):
            self.released = True

    fake_cv2 = types.SimpleNamespace(
        VideoCapture=Capture,
        CAP_PROP_FRAME_WIDTH=3,
        CAP_PROP_FRAME_HEIGHT=4,
        CAP_PROP_FRAME_COUNT=7,
        CAP_PROP_FPS=5,
    )
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)

    output = tmp_path / "550_test.mp4"
    output.write_bytes(b"mp4")

    assert ArmoredHub._video_metadata(output) == (1080, 1920, 10)


def test_publish_telegram_reconciles_timeout_without_republishing(monkeypatch, tmp_path):
    class TimeoutBot:
        def __init__(self, token, request):
            pass

        async def send_video(self, **kwargs):
            from telegram.error import TimedOut
            raise TimedOut("simulated timeout")

        async def shutdown(self):
            pass

    class FakeRequest:
        def __init__(self, **kwargs):
            pass

    fake_telegram = types.ModuleType("telegram")
    fake_telegram.Bot = TimeoutBot

    class FakeTimedOut(Exception):
        pass

    class FakeNetworkError(Exception):
        pass

    fake_error = types.ModuleType("telegram.error")
    fake_error.NetworkError = FakeNetworkError
    fake_error.TimedOut = FakeTimedOut

    fake_request = types.ModuleType("telegram.request")
    fake_request.HTTPXRequest = FakeRequest

    monkeypatch.setitem(sys.modules, "telegram", fake_telegram)
    monkeypatch.setitem(sys.modules, "telegram.error", fake_error)
    monkeypatch.setitem(sys.modules, "telegram.request", fake_request)

    monkeypatch.setenv("ARMORED_CREATOR_BOT_TOKEN", "test-token")
    monkeypatch.setenv("ARMORED_HUB_TOPIC_ID", "228")

    output = tmp_path / "550_test.mp4"
    output.write_bytes(b"mp4")
    item = _item(output)

    calls = {"started": 0, "checked": 0}

    class DB:
        def publication_started(self, *args, **kwargs):
            calls["started"] += 1

        def publication_send_started(self, *args, **kwargs):
            calls["send_started"] = calls.get("send_started", 0) + 1

        def publication(self, content_id):
            return {
                "published_message_id": "1234",
                "confirmed": 1,
            }

    hub = ArmoredHub(tmp_path, db=DB())
    hub._resolve_destination_chat_id = lambda topic_id: "-100123"
    hub._video_metadata = lambda _output: (1080, 1920, 8)

    def reconcile(_item):
        calls["checked"] += 1
        return PublicationCheck.CONFIRMED

    hub.check_publication = reconcile

    result = hub._publish_telegram(item, output)

    assert result.confirmed is True
    assert result.message_id == "1234"
    assert calls["started"] == 1
    assert calls["checked"] == 1


def test_publish_telegram_raises_publication_unknown_on_unresolved_timeout(monkeypatch, tmp_path):
    class TimeoutBot:
        def __init__(self, token, request):
            pass

        async def send_video(self, **kwargs):
            from telegram.error import TimedOut
            raise TimedOut("simulated timeout")

        async def shutdown(self):
            pass

    class FakeRequest:
        def __init__(self, **kwargs):
            pass

    fake_telegram = types.ModuleType("telegram")
    fake_telegram.Bot = TimeoutBot

    fake_error = types.ModuleType("telegram.error")
    fake_error.NetworkError = type("NetworkError", (Exception,), {})
    fake_error.TimedOut = type("TimedOut", (Exception,), {})

    fake_request = types.ModuleType("telegram.request")
    fake_request.HTTPXRequest = FakeRequest

    monkeypatch.setitem(sys.modules, "telegram", fake_telegram)
    monkeypatch.setitem(sys.modules, "telegram.error", fake_error)
    monkeypatch.setitem(sys.modules, "telegram.request", fake_request)

    monkeypatch.setenv("ARMORED_CREATOR_BOT_TOKEN", "test-token")
    monkeypatch.setenv("ARMORED_HUB_TOPIC_ID", "228")

    output = tmp_path / "550_test.mp4"
    output.write_bytes(b"mp4")
    item = _item(output)

    hub = ArmoredHub(tmp_path, db=types.SimpleNamespace(
        publication_started=lambda *args, **kwargs: None,
        publication_send_started=lambda *args, **kwargs: None,
        publication=lambda _content_id: {"published_message_id": None, "confirmed": 0},
    ))
    hub._resolve_destination_chat_id = lambda topic_id: "-100123"
    hub._video_metadata = lambda _output: (1080, 1920, 8)
    hub.check_publication = lambda _item: PublicationCheck.UNKNOWN

    from armored_core.services import PublicationUnknownError

    try:
        hub._publish_telegram(item, output)
    except PublicationUnknownError as exc:
        assert str(exc) == "Telegram publication outcome is UNKNOWN"
    else:
        raise AssertionError("expected PublicationUnknownError")


def test_publish_telegram_passes_real_video_metadata_to_send_video(monkeypatch, tmp_path):
    captured = {}

    class Capture:
        def __init__(self, path):
            pass

        def isOpened(self):
            return True

        def get(self, prop):
            return {
                3: 1080.0,
                4: 1920.0,
                7: 240.0,
                5: 24.0,
            }[prop]

        def release(self):
            pass

    fake_cv2 = types.SimpleNamespace(
        VideoCapture=Capture,
        CAP_PROP_FRAME_WIDTH=3,
        CAP_PROP_FRAME_HEIGHT=4,
        CAP_PROP_FRAME_COUNT=7,
        CAP_PROP_FPS=5,
    )
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)

    class FakeBot:
        def __init__(self, token, request):
            pass

        async def send_video(self, **kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(message_id=999)

        async def shutdown(self):
            pass

    class FakeRequest:
        def __init__(self, **kwargs):
            pass

    fake_telegram = types.ModuleType("telegram")
    fake_telegram.Bot = FakeBot

    fake_error = types.ModuleType("telegram.error")
    fake_error.NetworkError = type("NetworkError", (Exception,), {})
    fake_error.TimedOut = type("TimedOut", (Exception,), {})

    fake_request = types.ModuleType("telegram.request")
    fake_request.HTTPXRequest = FakeRequest

    monkeypatch.setitem(sys.modules, "telegram", fake_telegram)
    monkeypatch.setitem(sys.modules, "telegram.error", fake_error)
    monkeypatch.setitem(sys.modules, "telegram.request", fake_request)

    monkeypatch.setenv("ARMORED_CREATOR_BOT_TOKEN", "test-token")
    monkeypatch.setenv("ARMORED_HUB_TOPIC_ID", "228")

    output = tmp_path / "550_test.mp4"
    output.write_bytes(b"mp4")
    item = _item(output)

    hub = ArmoredHub(tmp_path, db=types.SimpleNamespace(
        publication_started=lambda *args, **kwargs: None,
        publication_send_started=lambda *args, **kwargs: None,
        publication_message_sent=lambda *args, **kwargs: None,
        publication_confirmed=lambda *args, **kwargs: None,
    ))
    hub._resolve_destination_chat_id = lambda topic_id: "-100123"
    hub.check_publication = lambda item: PublicationCheck.CONFIRMED

    result = hub._publish_telegram(item, output)

    assert isinstance(result, PublicationResult)
    assert result.message_id == "999"
    assert captured["duration"] == 10
    assert captured["width"] == 1080
    assert captured["height"] == 1920
    assert captured["supports_streaming"] is True
    assert captured["message_thread_id"] == 228
