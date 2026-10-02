from pathlib import Path
import json
import subprocess
import sys
from typing import Any

try:
    from .tool_paths import resolve_executable
except ImportError:
    from tool_paths import resolve_executable

VIDEO_PRESET = "medium"
VIDEO_CRF = "18"
VIDEO_PROFILE = "high"
VIDEO_LEVEL = "4.0"
VIDEO_PIX_FMT = "yuv420p"
AUDIO_BITRATE = "192k"

VOICE_VOLUME = 1.0
MUSIC_VOLUME = 0.8
INTRO_MUSIC_VOLUME = 2.5

INTRO_DURATION = 2.0
INTRO_WIDTH = 1080
INTRO_HEIGHT = 1920
STORY_WIDTH = 1080
STORY_HEIGHT = 1920
BLUR = "boxblur=6:2"
FADE_IN = "fade=t=in:st=0:d=0.6:color=white"
FADE_OUT = "fade=t=out:st=1.7:d=0.6:color=white"
BRIGHTNESS = "0.00"
CONTRAST = "1.10"
SATURATION = "1.12"
GAMMA = "1.00"
UNSHARP = "5:5:0.5:5:5:0.0"

# processing/finalizer.py -> project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _windows_hidden_kwargs():
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {"startupinfo": startupinfo, "creationflags": subprocess.CREATE_NO_WINDOW}


def validar_arquivo(arquivo, descricao):
    arquivo = Path(arquivo)
    if not arquivo.exists():
        raise FileNotFoundError(f"{descricao} não encontrado:\n{arquivo}")
    if arquivo.stat().st_size <= 0:
        raise RuntimeError(f"{descricao} está vazio:\n{arquivo}")


def probe_video(video):
    ffprobe = resolve_executable("ffprobe", env_var="ARMORED_FFPROBE", project_root=PROJECT_ROOT)
    comando = [str(ffprobe), "-v", "error", "-select_streams", "v:0",
               "-show_entries", "stream=width,height,r_frame_rate", "-of", "json", str(video)]
    resultado = subprocess.run(comando, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace",
                               **_windows_hidden_kwargs())
    if resultado.returncode != 0:
        raise RuntimeError("Não foi possível obter a resolução/FPS do vídeo:\n" + resultado.stderr.strip())
    streams = json.loads(resultado.stdout).get("streams") or []
    if not streams:
        raise RuntimeError("Vídeo sem stream de vídeo válido.")
    stream = streams[0]
    return int(stream["width"]), int(stream["height"]), stream.get("r_frame_rate", "30/1")


def _plan_video_filters(plan: dict[str, Any] | None, *, audio=False):
    plan = plan or {}
    if audio:
        start = float(plan.get("audio_start", 0) or 0)
        return [f"atrim=start={start:g}", "asetpts=PTS-STARTPTS"] if start > 0 else []
    filters = []
    start = float(plan.get("video_start", 0) or 0)
    if start > 0:
        filters += [f"trim=start={start:g}", "setpts=PTS-STARTPTS"]
    crop = plan.get("crop_final")
    if crop:
        x, y = int(crop.get("x", 0)), int(crop.get("y", 0))
        w, h = int(crop.get("width", 0)), int(crop.get("height", 0))
        if w <= 0 or h <= 0:
            raise ValueError("crop_final inválido.")
        filters.append(f"crop={w}:{h}:{x}:{y}")
    return filters


def _story_normalization_filters(width: int, height: int) -> list[str]:
    """Normaliza o vídeo principal para Story 9:16 sem deformar o conteúdo.

    A imagem é ampliada proporcionalmente até cobrir 1080x1920 e somente o
    excedente é cortado pelo centro. Assim, não há stretch nem padding/blur.
    """
    if width <= 0 or height <= 0:
        raise ValueError("Dimensões do vídeo inválidas.")
    return [
        f"scale={STORY_WIDTH}:{STORY_HEIGHT}:force_original_aspect_ratio=increase",
        f"crop={STORY_WIDTH}:{STORY_HEIGHT}:(iw-{STORY_WIDTH})/2:(ih-{STORY_HEIGHT})/2",
        "setsar=1",
    ]


def _plan_dimensions(plan, width, height):
    crop = (plan or {}).get("crop_final")
    if crop:
        return int(crop.get("width", width)), int(crop.get("height", height))
    return width, height


def criar_filtro(position, largura, altura, fps, plan=None):
    plan_video = _plan_video_filters(plan)
    base_video = plan_video + _story_normalization_filters(largura, altura) + [
        f"eq=brightness={BRIGHTNESS}:contrast={CONTRAST}:saturation={SATURATION}:gamma={GAMMA}",
        f"unsharp={UNSHARP}", "setpts=PTS-STARTPTS",
    ]
    visual = "[0:v]" + ",".join(base_video) + "[main_v]"

    intro_source = _plan_video_filters(plan)
    intro_source += [f"trim=duration={INTRO_DURATION}", "setpts=PTS-STARTPTS"]
    intro = (
        "[0:v]" + ",".join(intro_source) + ","
        f"scale={INTRO_WIDTH}:2133,"
        f"crop={INTRO_WIDTH}:{INTRO_HEIGHT}:x=(iw-{INTRO_WIDTH})/2:y=(ih-{INTRO_HEIGHT})/2,"
        f"{BLUR},{FADE_IN},{FADE_OUT}[intro_bg];"
        f"[3:v]scale={INTRO_WIDTH}:{INTRO_HEIGHT}[banner];"
        f"[intro_bg][banner]overlay=0:0,fps={fps},setpts=PTS-STARTPTS[intro_v0];"
        f"[intro_v0]trim=duration={INTRO_DURATION},setpts=PTS-STARTPTS,"
        f"scale={largura}:{altura}:force_original_aspect_ratio=increase,"
        f"crop={largura}:{altura}:(iw-{largura})/2:(ih-{altura})/2,"
        "setsar=1,setpts=PTS-STARTPTS[intro_v]"
    )

    audio_main = "[1:a]"
    audio_filters = _plan_video_filters(plan, audio=True)
    if audio_filters:
        audio_main += ",".join(audio_filters) + ","
    audio_main += (
        f"volume={VOICE_VOLUME}[voice];"
        f"[2:a]loudnorm=I=-12:LRA=7:TP=-1,volume={MUSIC_VOLUME}[music];"
        "[voice][music]amix=inputs=2:duration=first:dropout_transition=2[main_a]"
    )

    audio_intro = (
        f"[2:a]afade=t=in:st=0:d=0.4,afade=t=out:st=1.5:d=0.5,"
        f"loudnorm=I=-5:LRA=7:TP=-1,volume={INTRO_MUSIC_VOLUME},"
        f"atrim=duration={INTRO_DURATION},asetpts=PTS-STARTPTS[intro_a]"
    )
    concat_order = "[intro_v][intro_a][main_v][main_a]" if position == "inicio" else "[main_v][main_a][intro_v][intro_a]"
    concat = f"{concat_order}concat=n=2:v=1:a=1[vout][aout]"
    return ";".join([visual, intro, audio_main, audio_intro, concat])


def finalizar(video, voz, musica, banner, saida, position="final", intro=True, plan=None):
    video, voz, musica, banner, saida = map(Path, (video, voz, musica, banner, saida))
    validar_arquivo(video, "Vídeo")
    validar_arquivo(voz, "Áudio RVC")
    validar_arquivo(musica, "Música")
    if intro:
        validar_arquivo(banner, "Banner")
        if position not in {"inicio", "final"}:
            raise RuntimeError(f"Posição de intro inválida: {position}")

    largura, altura, fps = probe_video(video)
    largura, altura = _plan_dimensions(plan, largura, altura)
    output_largura, output_altura = STORY_WIDTH, STORY_HEIGHT
    saida.parent.mkdir(parents=True, exist_ok=True)
    if saida.exists():
        saida.unlink()

    if intro:
        filtro = criar_filtro(position, output_largura, output_altura, fps, plan)
        maps = ("[vout]", "[aout]")
    else:
        vf = _plan_video_filters(plan) + _story_normalization_filters(largura, altura) + [
            f"eq=brightness={BRIGHTNESS}:contrast={CONTRAST}:saturation={SATURATION}:gamma={GAMMA}",
            f"unsharp={UNSHARP}", "setpts=PTS-STARTPTS",
        ]
        af = _plan_video_filters(plan, audio=True)
        filtro = (
            "[0:v]" + ",".join(vf) + "[main_v];"
            + "[1:a]" + (",".join(af) + "," if af else "")
            + f"volume={VOICE_VOLUME}[voice];"
            f"[2:a]loudnorm=I=-12:LRA=7:TP=-1,volume={MUSIC_VOLUME}[music];"
            "[voice][music]amix=inputs=2:duration=first:dropout_transition=2[main_a]"
        )
        maps = ("[main_v]", "[main_a]")

    ffmpeg = resolve_executable("ffmpeg", env_var="ARMORED_FFMPEG", project_root=PROJECT_ROOT)
    comando = [str(ffmpeg), "-y", "-i", str(video), "-i", str(voz),
               "-stream_loop", "-1", "-i", str(musica)]
    if intro:
        comando += ["-loop", "1", "-i", str(banner)]
    comando += ["-filter_complex", filtro, "-map", maps[0], "-map", maps[1],
                "-c:v", "libx264", "-preset", VIDEO_PRESET, "-crf", VIDEO_CRF,
                "-profile:v", VIDEO_PROFILE, "-level", VIDEO_LEVEL, "-pix_fmt", VIDEO_PIX_FMT,
                "-g", "30", "-keyint_min", "30", "-sc_threshold", "0",
                "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-movflags", "+faststart", str(saida)]

    resultado = subprocess.run(comando, **_windows_hidden_kwargs())
    if resultado.returncode != 0:
        raise RuntimeError("Erro na finalização FFmpeg da V2.")
    validar_arquivo(saida, "Resultado final")
    return saida


def main():
    if len(sys.argv) not in {5, 7, 8}:
        print("Uso: python finalizer.py video.mp4 voz.wav musica.wav banner.png saida.mp4 [inicio|final] [intro_on|intro_off]")
        sys.exit(1)
    video, voz, musica, banner, saida = sys.argv[1:6]
    position = sys.argv[6] if len(sys.argv) >= 7 else "final"
    intro = True if len(sys.argv) < 8 else sys.argv[7].lower() != "intro_off"
    finalizar(video, voz, musica, banner, saida, position, intro)


if __name__ == "__main__":
    main()
