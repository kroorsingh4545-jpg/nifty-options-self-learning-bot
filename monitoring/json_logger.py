"""Structured JSON-lines logging with mandatory credential masking.

Every line carries: ``ts`` (IST ISO-8601), ``level``, ``logger``, ``event``,
``mode`` (PAPER/LIVE), ``strategy_id`` and ``order_id`` (null when not
relevant), plus any extra context fields. All string content, including
exception tracebacks, passes through the secret masker before it is written.

    configure_logging(level="INFO", mode=TradingMode.PAPER, log_dir=Path("logs"))
    log = get_logger(__name__, strategy_id="orb_options")
    log.info("signal generated", extra={"side": "CE", "score": 0.71})
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from collections.abc import MutableMapping
from datetime import datetime
from pathlib import Path
from typing import Any

from core.clock import IST
from core.enums import TradingMode
from core.masking import DEFAULT_MASKER, SecretMasker

_STD_ATTRS = frozenset(vars(logging.LogRecord("x", 0, "x", 0, "x", None, None))) | {"message", "asctime"}
_HANDLER_MARK = "_nifty_bot_handler"

_state: dict[str, Any] = {"mode": TradingMode.PAPER.value}


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    return repr(obj)


class JsonFormatter(logging.Formatter):
    """Render a ``LogRecord`` as one masked JSON object per line."""

    def __init__(self, masker: SecretMasker = DEFAULT_MASKER) -> None:
        super().__init__()
        self._masker = masker

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=IST).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            "mode": getattr(record, "mode", None) or _state["mode"],
            "strategy_id": getattr(record, "strategy_id", None),
            "order_id": getattr(record, "order_id", None),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and key not in payload and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        masked = self._masker.mask_obj(payload)
        line = json.dumps(masked, default=_json_default, ensure_ascii=False)
        # Final pass catches secrets inside non-string values rendered by repr().
        return self._masker.mask_text(line)


class ContextLogger(logging.LoggerAdapter[logging.Logger]):
    """Logger adapter that binds context (strategy_id, order_id, ...)."""

    def process(self, msg: Any, kwargs: MutableMapping[str, Any]) -> tuple[Any, MutableMapping[str, Any]]:
        extra = dict(self.extra or {})
        extra.update(kwargs.get("extra") or {})
        kwargs["extra"] = extra
        return msg, kwargs

    def bind(self, **context: Any) -> ContextLogger:
        merged = dict(self.extra or {})
        merged.update(context)
        return ContextLogger(self.logger, merged)


def get_logger(name: str, **context: Any) -> ContextLogger:
    return ContextLogger(logging.getLogger(name), context)


def configure_logging(
    *,
    level: str = "INFO",
    mode: TradingMode = TradingMode.PAPER,
    log_dir: Path | None = None,
    file_name: str = "bot.jsonl",
    console: bool = True,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 10,
    masker: SecretMasker = DEFAULT_MASKER,
    stream: Any = None,
) -> logging.Logger:
    """Install JSON handlers on the root logger (idempotent)."""
    _state["mode"] = TradingMode(mode).value
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            root.removeHandler(handler)
            handler.close()

    formatter = JsonFormatter(masker)
    handlers: list[logging.Handler] = []
    if console:
        handlers.append(logging.StreamHandler(stream or sys.stderr))
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_dir / file_name, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
            )
        )
    for handler in handlers:
        handler.setFormatter(formatter)
        setattr(handler, _HANDLER_MARK, True)
        root.addHandler(handler)
    root.setLevel(level)
    return root


def configure_from_config(cfg: Any, *, stream: Any = None) -> logging.Logger:
    """Configure logging from a loaded ``config.settings.BotConfig``."""
    log_cfg = cfg.settings.logging
    return configure_logging(
        level=log_cfg.level,
        mode=cfg.mode,
        log_dir=cfg.log_directory,
        file_name=log_cfg.file_name,
        console=log_cfg.console,
        max_bytes=log_cfg.max_bytes,
        backup_count=log_cfg.backup_count,
        stream=stream,
    )
