from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from armored_core.coordinator import Coordinator
from armored_core.database import Database
from armored_core.models import State
from armored_core.services import VisionResult, VisionUnresolvedError
from armored_core.storage import Storage


class _Reader:
    def __init__(self):
        self.events = []
        self.connected = False

    async def connect(self):
        self.events.append("connect")
        self.connected = True

    async def disconnect(self):
        self.events.append("disconnect")
        self.connected = False


class _Source:
    def __init__(self):
        self.reader = _Reader()


class _Vision:
    def __init__(self, events, unresolved=False):
        self.events = events
        self.unresolved = unresolved

    def identify(self, item):
        self.events.append("vision")
        if self.unresolved:
            raise VisionUnresolvedError("produto-nao-resolvido")
        return VisionResult(
            affiliate_name="produto",
            affiliate_url="https://shopee.com.br/product/123",
        )


class _Unused:
    def process(self, item):
        raise AssertionError("Studio não deve executar no gate")

    def publish(self, item):
        raise AssertionError("Hub não deve executar no gate")


def _coordinator(tmp_path: Path, monkeypatch, events, unresolved=False):
    monkeypatch.setenv("ARMORED_SOURCE_1_ID", "-1003788989075")
    monkeypatch.setenv("ARMORED_SOURCE_1_VIDEO_DIR", "Videos GRUPO_FONTE_1")
    storage = Storage(tmp_path)
    db = Database(storage.database / "test.db")
    source = _Source()
    coordinator = Coordinator(
        db,
        storage,
        _Vision(events, unresolved=unresolved),
        _Unused(),
        _Unused(),
        source=source,
    )
    return coordinator, db, storage


def test_vision_gate_runs_before_any_materialization(tmp_path, monkeypatch):
    events = []
    coordinator, db, storage = _coordinator(tmp_path, monkeypatch, events)

    async def materialize(target: Path):
        events.append("download")
        assert events == ["vision", "download"]
        expected_workspace = (
            storage.storage / "Videos GRUPO_FONTE_1" / "-1003788989075_77"
        )
        assert target.parent == expected_workspace
        target.write_bytes(b"telegram-video")

    message = SimpleNamespace(
        telegram_message_id="77",
        source_id="-1003788989075",
        topic_id=10,
        topic_name="source1",
        original_url="https://shopee.com.br/product/123",
        source_path=None,
        materialize=materialize,
    )

    item_id, materialized, item = asyncio.run(
        coordinator._vision_gate_and_materialize_async(message)
    )

    assert item_id == "-1003788989075_77"
    assert materialized is True
    assert events == ["vision", "download"]
    assert item.state == State.RECEIVED
    assert item.affiliate_url == "https://shopee.com.br/product/123"
    assert item.original_path == (
        storage.storage
        / "Videos GRUPO_FONTE_1"
        / "-1003788989075_77"
        / "-1003788989075_77_123.mp4"
    )
    assert item.original_path.is_file()
    assert not (storage.storage / "sources").exists()
    db.close()


def test_unresolved_vision_never_materializes(tmp_path, monkeypatch):
    events = []
    coordinator, db, storage = _coordinator(
        tmp_path, monkeypatch, events, unresolved=True
    )

    async def materialize(target: Path):
        events.append("download")
        target.write_bytes(b"must-not-happen")

    message = SimpleNamespace(
        telegram_message_id="78",
        source_id="-1003788989075",
        topic_id=10,
        topic_name="source1",
        original_url="https://shopee.com.br/product/456",
        source_path=None,
        materialize=materialize,
    )

    item_id, materialized, item = asyncio.run(
        coordinator._vision_gate_and_materialize_async(message)
    )

    assert item_id == "-1003788989075_78"
    assert materialized is False
    assert events == ["vision"]
    assert item.state == State.WAITING_VISION
    assert not item.original_path.exists()
    db.close()


def test_music_only_studio_mutes_original_audio_and_skips_rvc(tmp_path, monkeypatch):
    import ArmoredStudio.processing.finalizer as finalizer_module
    import ArmoredStudio.processing.rvc as rvc_module
    import ArmoredStudio.unified as unified_module

    storage = Storage(tmp_path)
    source = storage.storage / "Videos GRUPO_FONTE_2" / "88" / "88_original.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"video")

    music = tmp_path / "ArmoredStudio" / "assets" / "efeitosonoro.wav"
    banner = tmp_path / "ArmoredStudio" / "assets" / "banner.png"
    music.parent.mkdir(parents=True, exist_ok=True)
    music.write_bytes(b"music")
    banner.write_bytes(b"banner")

    item = SimpleNamespace(
        content_id="88",
        telegram_message_id="88",
        source_id="-1002698134896",
        original_path=source,
        working_path=None,
        affiliate_url="https://shopee.com.br/product/88",
        affiliate_name="produto",
    )

    class _Analysis:
        def analyze(self, _source):
            return SimpleNamespace(video={"duracao": 2.0}, plan={"ok": True})

    class _Profile:
        kind = unified_module.AudioKind.MUSIC_ONLY
        speech_present = False
        speech_ratio = 0.0
        rms_dbfs = -20.0

    monkeypatch.setenv("ARMORED_STUDIO_MUSIC", str(music))
    monkeypatch.setenv("ARMORED_STUDIO_BANNER", str(banner))
    monkeypatch.setenv("ARMORED_FFMPEG", "ffmpeg")

    commands = []
    captured = {}
    rvc_called = []

    def fake_run(command, **kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"SILENCE")
        return SimpleNamespace(returncode=0)

    def fail_rvc(*args, **kwargs):
        rvc_called.append((args, kwargs))
        raise AssertionError("RVC não pode ser executado para MUSIC_ONLY")

    def fake_finalize(video, voice, music_path, banner_path, output, **kwargs):
        captured["voice_bytes"] = Path(voice).read_bytes()
        captured["profile"] = kwargs["audio_profile"]
        Path(output).write_bytes(b"result")

    monkeypatch.setattr(unified_module, "analyze_audio", lambda *args, **kwargs: _Profile())
    monkeypatch.setattr(unified_module.shutil, "which", lambda _: "ffmpeg")
    monkeypatch.setattr(unified_module.subprocess, "run", fake_run)
    monkeypatch.setattr(rvc_module, "converter_voz", fail_rvc)
    monkeypatch.setattr(finalizer_module, "finalizar", fake_finalize)

    studio = unified_module.UnifiedStudio(
        tmp_path,
        storage,
        analysis=_Analysis(),
    )
    output, _ = studio.process(item)

    assert output.is_file()
    assert not rvc_called
    assert commands
    assert "-f" in commands[0] and "lavfi" in commands[0]
    assert "anullsrc=r=44100:cl=stereo" in commands[0]
    assert str(source) not in commands[0]
    assert captured["voice_bytes"] == b"SILENCE"
    assert captured["profile"].kind == unified_module.AudioKind.MUSIC_ONLY


def test_no_audio_studio_uses_same_no_speech_audio_path(tmp_path, monkeypatch):
    import ArmoredStudio.processing.finalizer as finalizer_module
    import ArmoredStudio.processing.rvc as rvc_module
    import ArmoredStudio.unified as unified_module

    storage = Storage(tmp_path)
    source = storage.storage / "Videos GRUPO_FONTE_1" / "99" / "99_original.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"video")
    music = tmp_path / "music.wav"
    banner = tmp_path / "banner.png"
    music.write_bytes(b"music")
    banner.write_bytes(b"banner")

    item = SimpleNamespace(
        content_id="99", telegram_message_id="99", source_id="-1003788989075",
        original_path=source, working_path=None,
        affiliate_url="https://shopee.com.br/product/99", affiliate_name="produto",
    )

    class _Analysis:
        def analyze(self, _source):
            return SimpleNamespace(video={"duracao": 2.0}, plan={"ok": True})

    class _Profile:
        kind = unified_module.AudioKind.NO_AUDIO
        speech_present = False
        speech_ratio = 0.0
        rms_dbfs = -120.0

    commands = []
    rvc_called = []
    captured = {}

    def fake_run(command, **kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"SILENCE")
        return SimpleNamespace(returncode=0)

    def fail_rvc(*args, **kwargs):
        rvc_called.append((args, kwargs))
        raise AssertionError("RVC não pode ser executado para NO_AUDIO")

    def fake_finalize(*args, **kwargs):
        captured["profile"] = kwargs["audio_profile"]
        Path(args[4]).write_bytes(b"result")

    monkeypatch.setenv("ARMORED_STUDIO_MUSIC", str(music))
    monkeypatch.setenv("ARMORED_STUDIO_BANNER", str(banner))
    monkeypatch.setenv("ARMORED_FFMPEG", "ffmpeg")
    monkeypatch.setattr(unified_module, "analyze_audio", lambda *args, **kwargs: _Profile())
    monkeypatch.setattr(unified_module.shutil, "which", lambda _: "ffmpeg")
    monkeypatch.setattr(unified_module.subprocess, "run", fake_run)
    monkeypatch.setattr(rvc_module, "converter_voz", fail_rvc)
    monkeypatch.setattr(finalizer_module, "finalizar", fake_finalize)

    studio = unified_module.UnifiedStudio(tmp_path, storage, analysis=_Analysis())
    output, _ = studio.process(item)

    assert output.is_file()
    assert not rvc_called
    assert any("anullsrc=r=44100:cl=stereo" in " ".join(cmd) for cmd in commands)
    assert not any(str(source) in cmd for cmd in commands if "anullsrc=r=44100:cl=stereo" in " ".join(cmd))
    assert captured["profile"].kind == unified_module.AudioKind.NO_AUDIO


def test_finalizer_uses_low_main_effect_for_narration_and_high_for_no_speech():
    from ArmoredStudio.processing.finalizer import (
        criar_filtro,
        INTRO_MUSIC_VOLUME,
        MUSIC_VOLUME,
    )

    class Speech:
        speech_present = True

    class NoSpeech:
        speech_present = False

    speech_filter = criar_filtro(
        "final", 1080, 1920, "30/1", plan={},
        intro_music_volume=INTRO_MUSIC_VOLUME,
        main_music_volume=MUSIC_VOLUME,
    )
    no_speech_filter = criar_filtro(
        "final", 1080, 1920, "30/1", plan={},
        intro_music_volume=INTRO_MUSIC_VOLUME,
        main_music_volume=INTRO_MUSIC_VOLUME,
    )

    assert f"volume={MUSIC_VOLUME:.3f}[music]" in speech_filter
    assert f"volume={INTRO_MUSIC_VOLUME:.3f}," in speech_filter
    assert f"volume={INTRO_MUSIC_VOLUME:.3f}[music]" in no_speech_filter
    assert f"volume={INTRO_MUSIC_VOLUME:.3f}," in no_speech_filter
