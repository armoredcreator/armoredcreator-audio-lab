from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

from armored_core.catch_up_stages import VALID_STAGES, run_catch_up_stage
from armored_core.coordinator import Coordinator


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Executa UMA etapa da coleta histórica ArmoredCreator e encerra. "
            "Não inicia Studio, IA, Hub, publicação ou LIVE."
        )
    )
    parser.add_argument("stage", choices=VALID_STAGES, help="sync, vision ou stock")
    args = parser.parse_args()

    root = Path(os.getenv("ARMORED_ROOT") or Path(__file__).resolve().parent).resolve()
    os.environ.setdefault("ARMORED_ROOT", str(root))
    os.environ.setdefault("ARMORED_REAL_TELEGRAM", "1")
    os.environ.setdefault("ARMORED_REQUIRED_SOURCE_COUNT", "3")

    logging.basicConfig(
        level=os.getenv("ARMORED_LOG_LEVEL", "WARNING").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
        force=True,
    )
    for logger_name in (
        "telethon", "telethon.network", "telethon.network.mtprotosender",
        "httpx", "httpcore", "asyncio", "torch", "transformers",
    ):
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    coordinator = Coordinator.build(root=root)
    try:
        # Use the same SQLite runtime lease as the production Coordinator.
        # Never run a staged operation concurrently with another process on this DB.
        coordinator.db.acquire_runtime_lock("coordinator")
        coordinator._runtime_lock_held = True

        routes = getattr(coordinator.source, "sources", ())
        if len(routes) != 3:
            raise RuntimeError(
                "Esperadas exatamente 3 fontes Telegram configuradas; "
                f"encontradas {len(routes)}."
            )
        logging.info(
            "[CATCH-UP] Etapa solicitada: %s. Ferramentas posteriores ficarão paradas.",
            args.stage.upper(),
        )
        report = asyncio.run(run_catch_up_stage(coordinator, args.stage))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["errors"] else 0
    except KeyboardInterrupt:
        logging.warning("[CATCH-UP] Interrompido pelo operador; estado durável preservado.")
        return 130
    except Exception as exc:
        logging.error("[CATCH-UP] Etapa encerrada: %s", exc)
        return 1
    finally:
        coordinator.close()


if __name__ == "__main__":
    raise SystemExit(main())
