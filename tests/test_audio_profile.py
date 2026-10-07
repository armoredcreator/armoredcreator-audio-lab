from __future__ import annotations

from ArmoredStudio.analysis.audio_profile import AudioKind, AudioProfile, _classify, intro_music_gain


def test_audio_classifier_detects_speech_independent_of_language():
    assert _classify(0.20, -20.0) == AudioKind.SPEECH
    assert _classify(0.12, -20.0) == AudioKind.SPEECH_PLUS_MUSIC


def test_audio_classifier_detects_music_only_and_silence():
    assert _classify(0.01, -20.0) == AudioKind.MUSIC_ONLY
    assert _classify(0.0, -60.0) == AudioKind.NO_AUDIO


def test_intro_gain_is_adaptive_and_bounded():
    speech = intro_music_gain(-12.0, True)
    music = intro_music_gain(-24.0, False)
    assert 0.35 <= speech <= 2.5
    assert 0.35 <= music <= 2.5
    assert speech != music


def test_audio_profile_speech_property():
    assert AudioProfile(AudioKind.SPEECH, 0.3, -20, -2).speech_present
    assert AudioProfile(AudioKind.MUSIC_ONLY, 0.0, -20, -2).speech_present is False
