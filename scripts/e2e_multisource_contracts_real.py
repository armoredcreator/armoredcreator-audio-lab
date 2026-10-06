from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
from dataclasses import replace
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from armored_core.coordinator import Coordinator
from armored_core.database import Database
from armored_core.models import PublicationCheck, State
from armored_core.routing import load_routes
from armored_core.storage import Storage
from ArmoredHub.service import ArmoredHub
from ArmoredStudio.analysis.audio_profile import AudioKind, analyze_audio
from ArmoredStudio.service import ArmoredStudio
import ArmoredStudio.processing.rvc as rvc_module
import ArmoredStudio.unified as unified_module
from ArmoredSync.service import TelegramReader, TelegramSource
from ArmoredVision.service import ArmoredVision
from ArmoredIA.service import ArmoredIA


def _load_credentials() -> None:
    credentials = ROOT / "credentials" / "project.env"
    if not credentials.is_file():
        raise RuntimeError(
            "credentials/project.env não encontrado. "
            "A certificação real exige o arquivo único de credenciais."
        )
    load_dotenv(credentials, override=True)


def _require_real_runtime(routes) -> None:
    if os.getenv("ARMORED_HUB_DRY_RUN", "0") == "1":
        raise RuntimeError(
            "ARMORED_HUB_DRY_RUN=1. Desative o dry-run para a certificação E2E real."
        )
    missing = [
        name
        for name in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "ARMORED_CREATOR_BOT_TOKEN")
        if not (os.getenv(name) or "").strip()
    ]
    if missing:
        raise RuntimeError(
            "Credenciais/configuração real ausentes: " + ", ".join(missing)
        )
    if len(routes) < 2:
        raise RuntimeError(
            "A certificação multisource exige Source 1 e Source 2 configuradas."
        )


def _production_has_candidate(
    prod_conn: sqlite3.Connection | None,
    source_id: str,
    message_id: str,
    url: str | None,
) -> bool:
    if prod_conn is None:
        return False
    row = prod_conn.execute(
        """
        SELECT 1
        FROM items
        WHERE source_id = ?
          AND (
                telegram_message_id = ?
                OR (
                    ? IS NOT NULL
                    AND LOWER(TRIM(original_url)) = LOWER(TRIM(?))
                )
          )
        LIMIT 1
        """,
        (str(source_id), str(message_id), url, url),
    ).fetchone()
    return row is not None


def _assert_isolated_workspace(storage: Storage, item) -> None:
    source_folder = (
        "Videos GRUPO_FONTE_2"
        if str(item.source_id) == "-1002698134896"
        else "Videos GRUPO_FONTE_1"
    )
    workspace = Path(item.workspace).resolve()
    expected = (
        storage.storage / source_folder / str(item.content_id)
    ).resolve()
    if workspace != expected:
        raise AssertionError(
            f"workspace incorreto: esperado={expected} atual={workspace}"
        )
    if not workspace.is_dir():
        raise AssertionError(f"workspace não existe: {workspace}")
    if (storage.storage / "sources").exists():
        raise AssertionError("storage/sources não pode existir neste contrato")


async def _gate_one(
    coordinator: Coordinator,
    source: TelegramSource,
    message,
) -> tuple[str, object | None]:
    events: list[str] = []
    original_identify = coordinator.pipeline.vision.identify
    original_materialize = message.materialize
    item_id = coordinator.db.content_id_for(
        str(message.telegram_message_id),
        str(message.source_id),
    )

    def identify(item):
        events.append("vision")
        return original_identify(item)

    coordinator.pipeline.vision.identify = identify

    async def materialize(target: Path):
        current = coordinator.db.get(item_id)
        if not current.affiliate_url:
            raise AssertionError(
                "DOWNLOAD ocorreu antes de a Vision persistir affiliate_url"
            )
        if events != ["vision"]:
            raise AssertionError(f"ordem inválida antes do download: {events}")
        events.append("download")
        result = original_materialize(target)
        if hasattr(result, "__await__"):
            await result

    gated_message = replace(message, materialize=materialize)
    try:
        result = await coordinator._vision_gate_and_materialize_async(gated_message)
    finally:
        coordinator.pipeline.vision.identify = original_identify

    actual_id, materialized, item = result
    source.mark_ingested(str(message.telegram_message_id))
    if not materialized:
        if getattr(item, "state", None) != State.WAITING_VISION:
            raise AssertionError(
                f"Vision não materializou e estado inesperado: {getattr(item, 'state', None)}"
            )
        return actual_id, None

    if events != ["vision", "download"]:
        raise AssertionError(f"contrato Vision->download violado: {events}")
    _assert_isolated_workspace(coordinator.storage, item)
    if not Path(item.original_path).is_file():
        raise AssertionError(f"ORIGINAL ausente após materialização: {item.original_path}")
    return actual_id, item


def _build_runtime(
    temp_root: Path,
    reader: TelegramReader,
    source,
    source_id: str,
    routes,
):
    storage = Storage(temp_root)
    db = Database(storage.database / "armoredcreator.db")
    for route in routes:
        db.initialize_source_state(route.source.source_id)
    vision = ArmoredVision()
    studio = ArmoredStudio(temp_root)
    ia = ArmoredIA()
    publisher = ArmoredHub(ROOT, db, routes=routes)
    source_obj = TelegramSource(
        temp_root,
        reader,
        db,
        source=source,
        source_id=source_id,
    )
    return Coordinator(
        db,
        storage,
        vision,
        studio,
        publisher,
        source=source_obj,
        ia=ia,
    )


async def _discover_unprocessed(
    source: TelegramSource,
    prod_conn: sqlite3.Connection | None,
    limit: int,
):
    for _ in range(limit):
        message = await source.fetch_next_async()
        if message is None:
            return None
        if _production_has_candidate(
            prod_conn,
            str(message.source_id),
            str(message.telegram_message_id),
            message.original_url,
        ):
            print(
                f"[CERT][SKIP] source={message.source_id} "
                f"message={message.telegram_message_id} já existe na produção"
            )
            source.mark_ingested(str(message.telegram_message_id))
            continue
        return message
    return None


async def _process_and_verify(
    coordinator: Coordinator,
    item_id: str,
    expected_chat: str,
    expected_topic: int,
):
    coordinator.run(item_id)
    item = coordinator.db.get(item_id)
    if item.state != State.PUBLISHED:
        raise AssertionError(f"item não terminou PUBLISHED: {item.state}")
    if not item.cleanup_completed:
        raise AssertionError("cleanup não foi concluído")

    publication = coordinator.db.publication(item_id)
    if publication is None:
        raise AssertionError("publication ausente no SQLite")
    if str(publication["destination_chat_id"]) != expected_chat:
        raise AssertionError(
            f"destination_chat_id incorreto: "
            f"{publication['destination_chat_id']} != {expected_chat}"
        )
    if int(publication["destination_topic_id"]) != expected_topic:
        raise AssertionError(
            f"destination_topic_id incorreto: "
            f"{publication['destination_topic_id']} != {expected_topic}"
        )
    message_id = str(publication["published_message_id"] or "")
    if not message_id:
        raise AssertionError("Telegram message_id real ausente")

    status = coordinator.pipeline.publisher.check_publication(item)
    if status != PublicationCheck.CONFIRMED:
        raise AssertionError(
            f"reconciliação Telegram não confirmou a publicação: {status}"
        )
    print(
        f"[CERT][HUB] item={item_id} -> chat={expected_chat} "
        f"tópico={expected_topic} Telegram message={message_id} CONFIRMED"
    )
    return item


async def main_async() -> int:
    _load_credentials()
    routes = load_routes()
    _require_real_runtime(routes)

    route1 = next(r for r in routes if str(r.source.key) == "source1")
    route2 = next(r for r in routes if str(r.source.key) == "source2")

    print("=" * 72)
    print("ARMORED CREATOR - E2E REAL DOS CONTRATOS MULTISOURCE")
    print("=" * 72)
    print(
        f"Source1: {route1.source.chat_id} -> "
        f"Hub {route1.hub.chat_id} tópico {route1.hub.topic_id}"
    )
    print(
        f"Source2: {route2.source.chat_id} -> "
        f"Hub {route2.hub.chat_id} tópico {route2.hub.topic_id}"
    )

    production_db_path = ROOT / "storage" / "database" / "armoredcreator.db"
    prod_conn = None
    if production_db_path.is_file():
        prod_conn = sqlite3.connect(
            f"file:{production_db_path}?mode=ro",
            uri=True,
            timeout=10,
        )
        prod_conn.execute("PRAGMA busy_timeout=10000")
        print(f"[CERT][DB] produção somente-leitura: {production_db_path}")
    else:
        print("[CERT][DB] produção não encontrada; seleção sem filtro histórico.")

    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    reader = TelegramReader(ROOT, api_id, api_hash)

    os.environ.setdefault(
        "ARMORED_STUDIO_MUSIC",
        str(ROOT / "ArmoredStudio" / "assets" / "efeitosonoro.wav"),
    )
    os.environ.setdefault(
        "ARMORED_STUDIO_BANNER",
        str(ROOT / "ArmoredStudio" / "assets" / "banner.png"),
    )

    try:
        with tempfile.TemporaryDirectory(prefix="armoredcreator-cert-") as temp_dir:
            temp_root = Path(temp_dir)

            print(
                "\n[1/3] SOURCE 1 - Vision antes do download + "
                "storage + publicação real"
            )
            coordinator1 = _build_runtime(
                temp_root / "source1",
                reader,
                route1.source.chat_id,
                route1.source.source_id,
                routes,
            )
            try:
                message1 = await _discover_unprocessed(
                    coordinator1.source,
                    prod_conn,
                    int(os.getenv("ARMORED_CERT_SOURCE_SCAN_LIMIT", "12")),
                )
                if message1 is None:
                    raise RuntimeError(
                        "Source 1 não encontrou candidato novo dentro do limite"
                    )

                item_id1, item1 = await _gate_one(
                    coordinator1,
                    coordinator1.source,
                    message1,
                )
                if item1 is None:
                    raise RuntimeError(
                        f"Source 1 encontrou WAITING_VISION no candidato {item_id1}; "
                        "certificação exige candidato resolvido."
                    )
                print(
                    f"[CERT][GATE] Source1 item={item_id1} "
                    f"Vision->download confirmado; ORIGINAL={item1.original_path}"
                )

                # Audio classification is a global Studio contract: every
                # source must pass through narration/music/no-audio detection.
                ffmpeg1 = os.getenv("ARMORED_FFMPEG", "ffmpeg")
                profile1 = analyze_audio(Path(item1.original_path), ffmpeg=ffmpeg1)
                print(
                    f"[CERT][AUDIO][SOURCE1] item={item_id1} "
                    f"AudioKind={profile1.kind.value} "
                    f"speech_present={profile1.speech_present}"
                )

                rvc_calls1 = []
                original_rvc1 = rvc_module.converter_voz
                original_run1 = unified_module.subprocess.run
                commands1 = []

                def recording_rvc1(*args, **kwargs):
                    rvc_calls1.append((args, kwargs))
                    return original_rvc1(*args, **kwargs)

                def recording_run1(command, *args, **kwargs):
                    commands1.append([str(value) for value in command])
                    return original_run1(command, *args, **kwargs)

                rvc_module.converter_voz = recording_rvc1
                unified_module.subprocess.run = recording_run1
                try:
                    await _process_and_verify(
                        coordinator1,
                        item_id1,
                        str(route1.hub.chat_id),
                        int(route1.hub.topic_id),
                    )
                finally:
                    rvc_module.converter_voz = original_rvc1
                    unified_module.subprocess.run = original_run1

                if profile1.speech_present:
                    if not rvc_calls1:
                        raise AssertionError(
                            "Source1 tem narração/voz, mas RVC não foi executado"
                        )
                    print("[CERT][AUDIO][SOURCE1] PASS: fala detectada -> RVC executado")
                else:
                    if rvc_calls1:
                        raise AssertionError(
                            "Source1 não tem narração, mas RVC foi executado"
                        )
                    silence_commands1 = [
                        command for command in commands1
                        if "anullsrc=r=44100:cl=stereo" in " ".join(command)
                    ]
                    if not silence_commands1:
                        raise AssertionError(
                            "Source1 sem narração não passou pelo caminho de silêncio"
                        )
                    if any(
                        str(item1.original_path) in command
                        for command in silence_commands1
                    ):
                        raise AssertionError(
                            "Source1 sem narração preservou o áudio original"
                        )
                    print(
                        "[CERT][AUDIO][SOURCE1] PASS: sem narração -> "
                        "sem RVC -> áudio original mutado"
                    )
            finally:
                coordinator1.close()

            print(
                "\n[2/3] SOURCE 2 - procurar candidato real MUSIC_ONLY "
                "sem baixar antes da Vision"
            )
            coordinator2 = _build_runtime(
                temp_root / "source2",
                reader,
                route2.source.chat_id,
                route2.source.source_id,
                routes,
            )

            rvc_called: list[tuple[tuple, dict]] = []
            original_rvc = rvc_module.converter_voz
            original_run = unified_module.subprocess.run
            captured_ffmpeg: list[list[str]] = []

            def forbidden_rvc(*args, **kwargs):
                rvc_called.append((args, kwargs))
                raise AssertionError("RVC foi chamado para MUSIC_ONLY")

            def capture_run(command, *args, **kwargs):
                captured_ffmpeg.append([str(value) for value in command])
                return original_run(command, *args, **kwargs)

            rvc_module.converter_voz = forbidden_rvc
            unified_module.subprocess.run = capture_run

            try:
                music_limit = int(
                    os.getenv("ARMORED_CERT_MUSIC_SCAN_LIMIT", "8")
                )
                music_item_id = None
                music_item = None

                for attempt in range(1, music_limit + 1):
                    message = await _discover_unprocessed(
                        coordinator2.source,
                        prod_conn,
                        1,
                    )
                    if message is None:
                        break

                    item_id, item = await _gate_one(
                        coordinator2,
                        coordinator2.source,
                        message,
                    )
                    if item is None:
                        print(
                            f"[CERT][MUSIC] tentativa={attempt} item={item_id} "
                            "Vision unresolved -> próxima candidata"
                        )
                        continue

                    ffmpeg = os.getenv("ARMORED_FFMPEG", "ffmpeg")
                    profile = analyze_audio(
                        Path(item.original_path),
                        ffmpeg=ffmpeg,
                    )
                    print(
                        f"[CERT][MUSIC] tentativa={attempt} item={item_id} "
                        f"AudioKind={profile.kind.value}"
                    )

                    if profile.kind == AudioKind.MUSIC_ONLY:
                        music_item_id = item_id
                        music_item = item
                        break

                if music_item is None:
                    raise RuntimeError(
                        "Nenhum candidato MUSIC_ONLY encontrado dentro de "
                        f"ARMORED_CERT_MUSIC_SCAN_LIMIT={music_limit}."
                    )

                print(
                    f"[CERT][MUSIC] candidato escolhido={nonspeech_item_id} "
                    "executando Studio real + pipeline completo"
                )
                await _process_and_verify(
                    coordinator2,
                    music_item_id,
                    str(route2.hub.chat_id),
                    int(route2.hub.topic_id),
                )

                if rvc_called:
                    raise AssertionError("RVC foi executado em MUSIC_ONLY")

                silence_commands = [
                    command
                    for command in captured_ffmpeg
                    if "anullsrc=r=44100:cl=stereo" in " ".join(command)
                ]
                if not silence_commands:
                    raise AssertionError(
                        "Studio não gerou silêncio para MUSIC_ONLY; nenhum comando "
                        "FFmpeg com anullsrc foi observado"
                    )
                if any(str(nonspeech_item.original_path) in command for command in silence_commands):
                    raise AssertionError(
                        "Studio preservou o áudio original no comando de MUSIC_ONLY"
                    )

                print(
                    "[CERT][AUDIO] PASS: vídeo sem narração -> sem RVC -> anullsrc -> "
                    "áudio original não usado na extração -> Studio/Hub/Telegram OK"
                )
            finally:
                rvc_module.converter_voz = original_rvc
                unified_module.subprocess.run = original_run
                coordinator2.close()

    finally:
        if prod_conn is not None:
            prod_conn.close()

    print("\n[3/3] CERTIFICAÇÃO")
    print("PASS: Vision antes de materialização para as duas fontes.")
    print("PASS: storage isolado em Videos GRUPO_FONTE_1 / Videos GRUPO_FONTE_2.")
    print("PASS: Source 1 publicado e confirmado no tópico 228.")
    print("PASS: Source 2 sem narração publicado e confirmado no tópico 1160.")
    print("PASS: MUSIC_ONLY não chamou RVC e mutou o áudio original.")
    print("PASS: banco de produção permaneceu somente-leitura.")
    print("RESULTADO: CERTIFICADO")
    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nCERTIFICAÇÃO interrompida pelo operador.")
        return 130
    except Exception as exc:
        print(f"\nRESULTADO: FAIL - {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
