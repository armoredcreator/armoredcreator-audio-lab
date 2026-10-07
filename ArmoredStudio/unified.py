from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .analysis.video import obter_informacoes_video
from .analysis.audio_intelligence import AudioIntelligence, AudioMode
from .analysis.blackbar import analisar_bordas_video
from .analysis.banner_analyzer import analisar_banner
from .analysis.banner import analisar_banner as analisar_corte_banner
from .analysis.veo_detector import detect_fast as detectar_veo
from .analysis.gemini_detector import detect_fast as detectar_gemini
from .analysis.export_planner import criar_plano_exportacao
from .analysis.export_plan_validator import validar_plano_exportacao


@dataclass(frozen=True)
class AnalysisResult:
    video: dict[str, Any]
    blackbar: dict[str, Any]
    banner: dict[str, Any]
    banner_cut: dict[str, Any]
    veo: dict[str, Any]
    gemini: dict[str, Any]
    plan: dict[str, Any]
    validation: dict[str, Any]
    audio: dict[str, Any] = field(default_factory=dict)


class AnalysisEngine:
    """Mandatory analysis stage. No video mutation happens here."""

    def __init__(self, audio_intelligence: AudioIntelligence | None = None):
        self.audio_intelligence = audio_intelligence or AudioIntelligence(
            os.getenv("ARMORED_FFMPEG", "ffmpeg")
        )

    def analyze(self, source: Path) -> AnalysisResult:
        video = obter_informacoes_video(source)
        if not video.get("sucesso"):
            raise RuntimeError(video.get("erro", "video-invalido"))

        blackbar = analisar_bordas_video(source)
        banner = analisar_banner(source)
        banner_cut = analisar_corte_banner(source) if banner.get("banner_detectado") else {}
        veo = detectar_veo(source)
        gemini = detectar_gemini(source)

        plan = criar_plano_exportacao(
            video,
            blackbar,
            banner,
            banner_cut,
            veo,
            gemini,
        )
        validation = validar_plano_exportacao(plan)
        if not validation.get("valido"):
            raise RuntimeError("Plano de processamento inválido: " + "; ".join(validation.get("erros", [])))

        audio = self.audio_intelligence.analyze(source)
        return AnalysisResult(video, blackbar, banner, banner_cut, veo, gemini, plan, validation, audio.as_dict())


class UnifiedStudio:
    """analysis + processing under one canonical Studio contract.

    The database/workspace remains outside this class. This class never creates
    storage/input, storage/output, queue or temp trees.
    """

    def __init__(self, root: Path, storage, *, analysis: AnalysisEngine | None = None):
        self.root = Path(root).resolve()
        self.storage = storage
        self.analysis = analysis or AnalysisEngine()

    def _test_copy(self, source: Path, output: Path) -> None:
        if os.getenv("ARMORED_STUDIO_ALLOW_COPY") != "1":
            raise RuntimeError("Modo de cópia de teste não habilitado")
        shutil.copy2(source, output)

    def process(self, item):
        original = Path(item.original_path)
        if not original.is_file():
            raise FileNotFoundError(original)

        output = self.storage.result(item.content_id, item.affiliate_url, item.affiliate_name, source_id=item.source_id)
        if output.exists():
            output.unlink()

        # The immutable ORIGINAL is the canonical Studio input. Do not create
        # a byte-identical WORKING copy merely to satisfy the old contract.
        # A WORKING artifact is used only when a durable intermediate exists.
        working = item.working_path
        source = Path(working) if working and Path(working).is_file() else original
        if not source.is_file():
            raise FileNotFoundError(source)

        # Explicit deterministic test mode preserves the contract tests without
        # pretending that arbitrary bytes are a real video.
        if os.getenv("ARMORED_STUDIO_FORCE_COPY") == "1":
            self._test_copy(source, output)
            return output, {"mode": "test-copy", "plan": None}

        if os.getenv("ARMORED_CONSOLE_VERBOSE", "0") == "1":
            analysis = self.analysis.analyze(source)
        else:
            with open(os.devnull, "w", encoding="utf-8") as quiet:
                with contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
                    analysis = self.analysis.analyze(source)

        # Real Studio resources are explicit/configurable; never use another
        # checkout or machine-specific path.
        music = Path(os.getenv("ARMORED_STUDIO_MUSIC", str(self.root / "ArmoredStudio" / "assets" / "efeitosonoro.wav")))
        banner = Path(os.getenv("ARMORED_STUDIO_BANNER", str(self.root / "ArmoredStudio" / "assets" / "banner.png")))

        if not music.is_file() or not banner.is_file():
            raise FileNotFoundError(
                "Recursos do Studio ausentes. Configure ARMORED_STUDIO_MUSIC e "
                "ARMORED_STUDIO_BANNER ou instale os assets no projeto."
            )

        ffmpeg = os.getenv("ARMORED_FFMPEG", "ffmpeg")
        if not shutil.which(ffmpeg):
            raise RuntimeError("FFmpeg não encontrado")

        # Audio Intelligence is a mandatory routing stage. The original audio
        # is classified before RVC so music-only and silent videos never enter
        # the voice converter.
        audio_info = analysis.audio
        audio_mode = str(audio_info.get("mode") or AudioMode.MUSIC_ONLY)
        duration = max(0.1, float(analysis.video.get("duracao") or audio_info.get("duration_seconds") or 0.1))
        audio_original = source.with_name(f"{item.telegram_message_id}_audio_original.wav")
        audio_rvc = source.with_name(f"{item.telegram_message_id}_audio_rvc.wav")

        try:
            if audio_mode in {AudioMode.NO_AUDIO, AudioMode.MUSIC_ONLY}:
                logging.getLogger(__name__).info(
                    "[STUDIO][ITEM %s] Audio Intelligence=%s; RVC ignorado",
                    item.content_id,
                    audio_mode,
                )
                subprocess.run(
                    [
                        ffmpeg, "-y",
                        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                        "-t", f"{duration:.3f}",
                        "-ac", "2", "-ar", "44100",
                        str(audio_rvc),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            else:
                extract_args = [
                    ffmpeg, "-y", "-i", str(source), "-vn",
                    "-ac", "2", "-ar", "44100",
                ]
                # Mixed speech+music gets a speech-focused preconditioning pass
                # before RVC. It is intentionally not described as perfect source
                # separation; Studio still controls the final background music.
                if audio_mode == AudioMode.SPEECH_PLUS_MUSIC:
                    extract_args.extend([
                        "-af",
                        "pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1,highpass=f=100,lowpass=f=5000",
                    ])
                extract_args.append(str(audio_original))
                subprocess.run(
                    extract_args,
                    check=True,
                    capture_output=True,
                    text=True,
                )

                voice = os.getenv("ARMORED_STUDIO_RVC_VOICE", "melody")
                logging.getLogger(__name__).info(
                    "[STUDIO][ITEM %s] Audio Intelligence=%s; RVC iniciando voz=%s",
                    item.content_id,
                    audio_mode,
                    voice,
                )
                from .processing.rvc import converter_voz
                converter_voz(audio_original, audio_rvc, voice, item_id=item.content_id)
                logging.getLogger(__name__).info(
                    "[STUDIO][ITEM %s] RVC concluído",
                    item.content_id,
                )

            from .processing.finalizer import finalizar
            finalizar(
                source,
                audio_rvc,
                music,
                banner,
                output,
                position=os.getenv("ARMORED_STUDIO_INTRO_POSITION", "final"),
                intro=os.getenv("ARMORED_STUDIO_INTRO", "1") != "0",
                plan=analysis.plan,
            )
        finally:
            for artifact in (audio_original, audio_rvc):
                artifact.unlink(missing_ok=True)

        if not output.is_file() or output.stat().st_size <= 0:
            raise RuntimeError("Studio produziu uma saída inválida")

        return output, analysis
