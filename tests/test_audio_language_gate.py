from pathlib import Path

import numpy as np

from ArmoredStudio.analysis.audio_intelligence import (
    AudioIntelligence,
    AudioIntelligenceResult,
    AudioMode,
)


def _speech_result(_cls, _samples, duration):
    return AudioIntelligenceResult(
        AudioMode.SPEECH,
        0.95,
        duration,
        0.8,
        0.05,
    )


def _analyzer(monkeypatch, language, confidence):
    analyzer = AudioIntelligence()
    monkeypatch.setattr(
        analyzer,
        "_decode",
        lambda _source: (np.ones(16000 * 4, dtype=np.float32) * 0.1, 4.0),
    )
    monkeypatch.setattr(
        AudioIntelligence,
        "_classify_samples",
        classmethod(_speech_result),
    )
    monkeypatch.setattr(
        analyzer,
        "_detect_language",
        lambda _samples: (language, confidence),
    )
    return analyzer


def test_portuguese_speech_remains_eligible_for_rvc(tmp_path: Path, monkeypatch):
    source = tmp_path / "speech.mp4"
    source.write_bytes(b"placeholder")

    result = _analyzer(monkeypatch, "pt", 0.91).analyze(source)

    assert result.mode == AudioMode.SPEECH
    assert result.language == "pt"
    assert result.language_confidence == 0.91


def test_foreign_speech_is_classified_separately_from_portuguese(tmp_path: Path, monkeypatch):
    source = tmp_path / "foreign.mp4"
    source.write_bytes(b"placeholder")

    result = _analyzer(monkeypatch, "en", 0.94).analyze(source)

    assert result.mode == AudioMode.FOREIGN_SPEECH
    assert result.language == "en"


def test_unverified_speech_fails_closed_away_from_rvc(tmp_path: Path, monkeypatch):
    source = tmp_path / "unknown.mp4"
    source.write_bytes(b"placeholder")

    result = _analyzer(monkeypatch, "unknown", 0.0).analyze(source)

    assert result.mode == AudioMode.SPEECH_UNVERIFIED
    assert result.language == "unknown"
