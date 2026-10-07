from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


class AudioMode:
    NO_AUDIO = "NO_AUDIO"
    MUSIC_ONLY = "MUSIC_ONLY"
    SPEECH = "SPEECH"
    SPEECH_PLUS_MUSIC = "SPEECH_PLUS_MUSIC"


@dataclass(frozen=True)
class AudioIntelligenceResult:
    mode: str
    confidence: float
    duration_seconds: float
    speech_ratio: float
    music_ratio: float
    method: str = "spectral-heuristic-v1"

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "confidence": round(float(self.confidence), 3),
            "duration_seconds": round(float(self.duration_seconds), 3),
            "speech_ratio": round(float(self.speech_ratio), 3),
            "music_ratio": round(float(self.music_ratio), 3),
            "method": self.method,
        }


def _windows_hidden_kwargs() -> dict[str, Any]:
    import sys
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {"startupinfo": startupinfo, "creationflags": subprocess.CREATE_NO_WINDOW}


class AudioIntelligence:
    """Classify the source audio before Studio/RVC processing.

    The classifier is deliberately conservative: RVC is enabled only when
    speech evidence is strong enough. No-audio and music-only inputs never
    enter RVC. Mixed speech+music is kept as a distinct durable mode so the
    Studio path can treat it differently from pure speech.
    """

    SAMPLE_RATE = 16000
    MAX_SECONDS = 45.0
    FRAME = 400
    HOP = 160

    def __init__(self, ffmpeg: str | None = None):
        self.ffmpeg = ffmpeg or os.getenv("ARMORED_FFMPEG", "ffmpeg")

    def _decode(self, source: Path) -> tuple[np.ndarray, float]:
        command = [
            self.ffmpeg, "-v", "error", "-i", str(source),
            "-vn", "-ac", "1", "-ar", str(self.SAMPLE_RATE),
            "-t", str(self.MAX_SECONDS), "-f", "s16le", "pipe:1",
        ]
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_windows_hidden_kwargs(),
        )
        raw = result.stdout or b""
        if not raw:
            return np.zeros(0, dtype=np.float32), 0.0
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        return samples, len(samples) / self.SAMPLE_RATE

    @classmethod
    def _classify_samples(cls, samples: np.ndarray, duration: float) -> AudioIntelligenceResult:
        if samples.size == 0 or duration <= 0:
            return AudioIntelligenceResult(AudioMode.NO_AUDIO, 1.0, duration, 0.0, 0.0)

        rms = float(np.sqrt(np.mean(np.square(samples)) + 1e-12))
        if rms < 0.006:
            return AudioIntelligenceResult(AudioMode.NO_AUDIO, 0.995, duration, 0.0, 0.0)

        frame_count = 1 + max(0, (len(samples) - cls.FRAME) // cls.HOP)
        if frame_count <= 0:
            return AudioIntelligenceResult(AudioMode.MUSIC_ONLY, 0.55, duration, 0.0, 1.0)

        window = np.hanning(cls.FRAME).astype(np.float32)
        speech_votes = 0
        music_votes = 0
        voiced_energy = 0.0
        total_energy = 0.0

        for index in range(frame_count):
            start = index * cls.HOP
            frame = samples[start:start + cls.FRAME]
            if len(frame) < cls.FRAME:
                frame = np.pad(frame, (0, cls.FRAME - len(frame)))
            frame = frame * window

            energy = float(np.mean(frame * frame))
            if energy < 1e-5:
                continue
            total_energy += energy

            spectrum = np.abs(np.fft.rfft(frame)) + 1e-8
            power = spectrum * spectrum
            freqs = np.fft.rfftfreq(cls.FRAME, 1.0 / cls.SAMPLE_RATE)
            spectral_sum = float(np.sum(power)) + 1e-12
            centroid = float(np.sum(freqs * power) / spectral_sum)
            bandwidth = float(
                np.sqrt(np.sum(((freqs - centroid) ** 2) * power) / spectral_sum)
            )
            flatness = float(np.exp(np.mean(np.log(spectrum))) / (np.mean(spectrum) + 1e-12))

            signs = np.signbit(frame)
            zcr = float(np.count_nonzero(signs[1:] != signs[:-1]) / max(1, len(frame) - 1))

            band = (freqs >= 180.0) & (freqs <= 4200.0)
            band_energy = float(np.sum(power[band]) / spectral_sum)

            # Conservative speech evidence: speech tends to occupy a broad
            # mid-band with moderate ZCR and less purely harmonic spectra.
            speech_score = (
                0.40 * (1.0 if 250.0 <= centroid <= 3200.0 else 0.0)
                + 0.20 * (1.0 if 0.025 <= zcr <= 0.22 else 0.0)
                + 0.20 * (1.0 if 0.45 <= band_energy <= 0.98 else 0.0)
                + 0.10 * (1.0 if flatness >= 0.12 else 0.0)
                + 0.10 * (1.0 if bandwidth >= 250.0 else 0.0)
            )

            # Music evidence is strongest for sustained harmonic/tonal energy.
            music_score = (
                0.45 * (1.0 if flatness < 0.20 else 0.0)
                + 0.30 * (1.0 if 500.0 <= centroid <= 7000.0 else 0.0)
                + 0.25 * (1.0 if band_energy >= 0.35 else 0.0)
            )

            if speech_score >= 0.62:
                speech_votes += 1
                voiced_energy += energy
            if music_score >= 0.62:
                music_votes += 1

        active_frames = max(1, frame_count)
        speech_ratio = speech_votes / active_frames
        music_ratio = music_votes / active_frames
        energy_speech_ratio = voiced_energy / max(total_energy, 1e-12)

        if speech_ratio >= 0.30 and music_ratio >= 0.25:
            confidence = min(0.99, 0.55 + 0.45 * min(1.0, speech_ratio + music_ratio))
            return AudioIntelligenceResult(
                AudioMode.SPEECH_PLUS_MUSIC,
                confidence,
                duration,
                speech_ratio,
                music_ratio,
            )

        if speech_ratio >= 0.30 and energy_speech_ratio >= 0.18:
            confidence = min(0.99, 0.55 + 0.45 * speech_ratio)
            return AudioIntelligenceResult(
                AudioMode.SPEECH,
                confidence,
                duration,
                speech_ratio,
                music_ratio,
            )

        confidence = min(0.99, 0.55 + 0.45 * max(music_ratio, 0.35))
        return AudioIntelligenceResult(
            AudioMode.MUSIC_ONLY,
            confidence,
            duration,
            speech_ratio,
            music_ratio,
        )

    def analyze(self, source: Path) -> AudioIntelligenceResult:
        source = Path(source)
        if not source.is_file():
            raise FileNotFoundError(source)
        samples, duration = self._decode(source)
        return self._classify_samples(samples, duration)
