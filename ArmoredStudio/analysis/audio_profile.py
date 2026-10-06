from __future__ import annotations

import math
import shutil
import subprocess
from array import array
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class AudioKind(StrEnum):
    NO_AUDIO = "NO_AUDIO"
    MUSIC_ONLY = "MUSIC_ONLY"
    SPEECH = "SPEECH"
    SPEECH_PLUS_MUSIC = "SPEECH_PLUS_MUSIC"


@dataclass(frozen=True)
class AudioProfile:
    kind: AudioKind
    speech_ratio: float
    rms_dbfs: float
    peak_dbfs: float

    @property
    def speech_present(self) -> bool:
        return self.kind in {AudioKind.SPEECH, AudioKind.SPEECH_PLUS_MUSIC}


def _dbfs(rms: float) -> float:
    if rms <= 1e-9:
        return -120.0
    return max(-120.0, 20.0 * math.log10(rms))


def _classify(speech_ratio: float, rms_dbfs: float) -> AudioKind:
    if rms_dbfs <= -55.0:
        return AudioKind.NO_AUDIO
    if speech_ratio >= 0.08:
        # VAD is language-agnostic: English, Spanish, Portuguese, etc. are
        # all treated as narration/speech and can safely enter the RVC stage.
        return AudioKind.SPEECH_PLUS_MUSIC if speech_ratio < 0.35 else AudioKind.SPEECH
    return AudioKind.MUSIC_ONLY


def analyze_audio(source: Path, *, ffmpeg: str = "ffmpeg", vad_mode: int = 3) -> AudioProfile:
    """Profile source audio without assuming a language.

    WebRTC VAD classifies voiced/unvoiced frames, while RMS/peak provide a
    deterministic level measurement. This intentionally detects speech rather
    than trying to guess a language: foreign narration is still narration.
    """
    executable = shutil.which(ffmpeg) or ffmpeg
    proc = subprocess.run(
        [
            executable, "-v", "error", "-i", str(source),
            "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1",
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=True,
    )
    samples = array("h", proc.stdout)
    if not samples:
        return AudioProfile(AudioKind.NO_AUDIO, 0.0, -120.0, -120.0)

    scale = 32768.0
    rms = math.sqrt(sum((sample / scale) ** 2 for sample in samples) / len(samples))
    peak = max(abs(sample) for sample in samples) / scale

    try:
        import webrtcvad
    except ImportError as exc:
        raise RuntimeError(
            "webrtcvad-wheels é obrigatório para detectar narração/voz"
        ) from exc

    vad = webrtcvad.Vad(int(vad_mode))
    frame_samples = 16000 * 30 // 1000
    frame_bytes = frame_samples * 2
    voiced = total = 0
    raw = proc.stdout
    for offset in range(0, len(raw) - frame_bytes + 1, frame_bytes):
        frame = raw[offset:offset + frame_bytes]
        total += 1
        if vad.is_speech(frame, 16000):
            voiced += 1

    ratio = voiced / total if total else 0.0
    return AudioProfile(
        kind=_classify(ratio, _dbfs(rms)),
        speech_ratio=ratio,
        rms_dbfs=_dbfs(rms),
        peak_dbfs=_dbfs(peak),
    )


def intro_music_gain(main_rms_dbfs: float, speech_present: bool) -> float:
    """Set intro/final music gain relative to the source loudness.

    The old fixed 2.5 multiplier is retained as the upper bound. Speech gets
    a lower target so the intro does not dominate narration; music-only
    sources can use a stronger effect. Bounds prevent pathological source
    levels from producing silence or clipping.
    """
    target_dbfs = -18.0 if speech_present else -14.0
    gain = 10.0 ** ((target_dbfs - float(main_rms_dbfs)) / 20.0)
    return max(0.35, min(float(2.5), gain))
