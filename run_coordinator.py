from __future__ import annotations

import logging
import os
from pathlib import Path

from armored_core.coordinator import Coordinator


def main() -> int:
    root = Path(os.getenv("ARMORED_ROOT") or Path(__file__).resolve().parent).resolve()
    os.environ.setdefault("ARMORED_ROOT", str(root))
    # O processo real do Coordinator deve usar o ArmoredSync/Telegram real.
    # LocalSource continua disponível apenas para testes offline via Coordinator.build().
    os.environ.setdefault("ARMORED_REAL_TELEGRAM", "1")

    logging.basicConfig(
        level=os.getenv("ARMORED_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
        force=True,
    )
    # Console is the operator/certification surface. Keep noisy third-party
    # internals out of it without hiding application warnings/errors.
    for logger_name in (
        "telethon",
        "telethon.network",
        "telethon.network.mtprotosender",
        "httpx",
        "httpcore",
        "asyncio",
        "fairseq",
        "rvc_python",
        "torch",
        "transformers",
    ):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    coordinator = Coordinator.build(root=root)

    required = ["ARMORED_CREATOR_BOT_TOKEN", "ARMORED_HUB_TOPIC_ID"]
    if os.getenv("ARMORED_IA_ENABLED", "1") == "1" and os.getenv("ARMORED_IA_CAPTION_ENABLED", "1") == "1":
        required.append("GEMINI_API_KEY")
    missing = [name for name in required if not (os.getenv(name) or "").strip()]
    if missing:
        raise RuntimeError(
            "Configuração obrigatória ausente em credentials/project.env: "
            + ", ".join(missing)
        )

    try:
        logging.info("ArmoredCreator Coordinator iniciado")
        logging.info("Root: %s", root)
        logging.info("Modo SQLite: %s", coordinator.db.sync_mode())
        logging.info("Sync real Telegram: %s", os.getenv("ARMORED_REAL_TELEGRAM"))
        logging.info("Hub dry-run efetivo: %s", os.getenv("ARMORED_HUB_DRY_RUN", "0"))
        logging.info("Fonte Sync: %s", os.getenv("ARMORED_SYNC_SOURCE") or "<não configurada>")
        max_cycles_raw = os.getenv("ARMORED_MAX_CYCLES")
        max_cycles = int(max_cycles_raw) if max_cycles_raw else None
        coordinator.run_forever(
            poll_seconds=float(os.getenv("ARMORED_POLL_SECONDS", "2")),
            max_cycles=max_cycles,
        )
        return 0
    except KeyboardInterrupt:
        logging.info("Shutdown solicitado pelo operador")
        return 0
    except SystemExit:
        return 0
    except Exception:
        logging.exception("Coordinator encerrou com erro")
        return 1
    finally:
        coordinator.close()


if __name__ == "__main__":
    raise SystemExit(main())
