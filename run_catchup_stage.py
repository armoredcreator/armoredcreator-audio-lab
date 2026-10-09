from __future__ import annotations

import argparse
import asyncio
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
        level=os.getenv("ARMORED_LOG_LEVEL", "ERROR").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
        force=True,
    )
    for logger_name in (
        "telethon", "telethon.network", "telethon.network.mtprotosender",
        "httpx", "httpcore", "asyncio", "torch", "transformers",
    ):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    # The stage runner reports one concise item error itself; suppress duplicate
    # pipeline stack/log lines while keeping unrelated critical failures visible.
    logging.getLogger("armored_core.pipeline").setLevel(logging.CRITICAL)
    logging.getLogger("armored_core.services").setLevel(logging.CRITICAL)
    logging.getLogger("ArmoredSync.service").setLevel(logging.CRITICAL)
    logging.getLogger("armored_core.catch_up_stages").setLevel(logging.CRITICAL)

    coordinator = None
    try:
        # Acquire the SQLite lease inside the composition root, before it
        # initializes source state or performs any staged database writes.
        coordinator = Coordinator.build(root=root, acquire_runtime_lock=True, staged=True)

        routes = getattr(coordinator.source, "sources", ())
        if len(routes) != 3:
            raise RuntimeError(
                "Esperadas exatamente 3 fontes Telegram configuradas; "
                f"encontradas {len(routes)}."
            )
        print(f"[CATCH-UP] INÍCIO etapa={args.stage.upper()}")
        report = asyncio.run(run_catch_up_stage(coordinator, args.stage))

        source_counts = report.get("per_source", {})
        source_summary = ",".join(
            f"F{index}[{route.source_id}]={source_counts.get(str(route.source_id), 0)}"
            for index, route in enumerate(routes, start=1)
        )
        outcomes = report.get("outcomes", {})
        if args.stage == "sync":
            detail = (
                f"reservas={outcomes.get('reserved', report.get('processed', 0))} "
                f"varredura={'OK' if report.get('historical_scan_exhausted') else 'INCOMPLETA'}"
            )
        elif args.stage == "vision":
            detail = (
                f"aprovados_novos={outcomes.get('approved_this_stage', 0)} "
                f"waiting_novos={outcomes.get('waiting_vision_this_stage', 0)} "
                f"aprovados_total={outcomes.get('approved_total', 0)} "
                f"waiting_total={outcomes.get('waiting_vision_total', 0)} "
                f"pendentes={outcomes.get('retryable_without_decision_total', 0)}"
            )
        else:
            detail = (
                f"baixados={outcomes.get('downloaded', report.get('processed', 0))} "
                f"originais_existentes={outcomes.get('skipped_existing_original', 0)} "
                f"faltantes={outcomes.get('approved_missing_original', 0)}"
            )

        errors = report.get("errors", [])
        status = "FALHOU" if errors else "OK"
        print(
            f"[CATCH-UP] {status} etapa={args.stage.upper()} "
            f"processados={report.get('processed', 0)} {source_summary} "
            f"{detail} erros={len(errors)} checkpoint=preservado"
        )
        for error in errors[:10]:
            print(f"[CATCH-UP][ERRO] {error}")
        if len(errors) > 10:
            print(f"[CATCH-UP][ERRO] ... mais {len(errors) - 10} erro(s) omitido(s)")
        return 1 if errors else 0
    except KeyboardInterrupt:
        logging.warning("[CATCH-UP] Interrompido pelo operador; estado durável preservado.")
        return 130
    except Exception as exc:
        logging.error("[CATCH-UP] Etapa encerrada: %s", exc)
        return 1
    finally:
        if coordinator is not None:
            coordinator.close()


if __name__ == "__main__":
    raise SystemExit(main())
