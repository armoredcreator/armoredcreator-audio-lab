"""Central Studio logger without legacy config dependencies."""
from __future__ import annotations
import logging
import os
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
_LOGS_DIR = Path(os.getenv("ARMORED_STUDIO_LOG_DIR", str(_ROOT.parent / "storage" / "logs"))).expanduser().resolve()
_LOG_FILE = _LOGS_DIR / "sistema.log"
def criar_logger():
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ArmoredStudio")
    logger.setLevel(logging.DEBUG)
    if logger.handlers:
        return logger
    fh = logging.FileHandler(_LOG_FILE, encoding="utf-8")
    ch = logging.StreamHandler()
    fh.setLevel(logging.DEBUG); ch.setLevel(logging.WARNING)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh.setFormatter(fmt); ch.setFormatter(fmt)
    logger.addHandler(fh); logger.addHandler(ch); logger.propagate = False
    return logger
log = criar_logger()
