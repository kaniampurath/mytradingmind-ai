from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
from typing import Any

from sqlalchemy.engine import make_url

from aegis_trader.core.config import settings


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(level: int | str | None = None) -> Path:
    log_level = _resolve_level(level or settings.log_level)
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "mytradingmind.log"

    root = logging.getLogger()
    root.setLevel(log_level)
    formatter = logging.Formatter(LOG_FORMAT)

    if not _has_handler(root, logging.StreamHandler, None):
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        stream_handler.setLevel(log_level)
        root.addHandler(stream_handler)

    if not _has_handler(root, RotatingFileHandler, log_path):
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(log_level)
        root.addHandler(file_handler)

    logging.getLogger("aegis_trader").info(
        "logging_configured path=%s level=%s max_bytes=%s backups=%s",
        log_path,
        logging.getLevelName(log_level),
        settings.log_max_bytes,
        settings.log_backup_count,
    )
    return log_path


def redact_url(url: str) -> str:
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return url


def log_diagnostic(logger: logging.Logger, event: str, **fields: Any) -> None:
    logger.info("%s %s", event, _format_fields(fields))


def _resolve_level(level: int | str) -> int:
    if isinstance(level, int):
        return level
    return int(getattr(logging, str(level).upper(), logging.INFO))


def _has_handler(root: logging.Logger, handler_type: type[logging.Handler], path: Path | None) -> bool:
    for handler in root.handlers:
        if not isinstance(handler, handler_type):
            continue
        if path is None:
            if not isinstance(handler, RotatingFileHandler):
                return True
            continue
        if isinstance(handler, RotatingFileHandler) and Path(handler.baseFilename) == path.resolve():
            return True
    return False


def _format_fields(fields: dict[str, Any]) -> str:
    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        text = str(value).replace("\n", "\\n")
        parts.append(f"{key}={text}")
    return " ".join(parts)
