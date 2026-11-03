"""Structured JSON logging."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from core.enums import TradingMode
from core.masking import DEFAULT_MASKER
from monitoring.json_logger import configure_logging, get_logger

FAKE_TOKEN = "fake-access-token-ZYXWVUTSRQPONMLKJIHG"


@pytest.fixture
def stream() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    configure_logging(level="DEBUG", mode=TradingMode.PAPER, console=True, stream=buf)
    yield buf
    configure_logging(console=False)  # detach test handlers


def _lines(buf: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def test_log_line_is_json_with_required_fields(stream: io.StringIO) -> None:
    get_logger("test.module").info("hello")
    (rec,) = _lines(stream)
    assert rec["event"] == "hello"
    assert rec["level"] == "INFO"
    assert rec["logger"] == "test.module"
    assert rec["mode"] == "PAPER"
    assert "strategy_id" in rec and rec["strategy_id"] is None
    assert "order_id" in rec and rec["order_id"] is None


def test_timestamp_is_ist(stream: io.StringIO) -> None:
    get_logger("t").info("x")
    ts = datetime.fromisoformat(_lines(stream)[0]["ts"])
    assert ts.utcoffset() is not None
    assert ts.utcoffset().total_seconds() == 5.5 * 3600  # type: ignore[union-attr]


def test_bound_context_and_extra_fields(stream: io.StringIO) -> None:
    log = get_logger("t", strategy_id="orb_options")
    log.bind(order_id="PAPER-orb_options-abc").info("order created", extra={"qty": 75, "side": "BUY"})
    rec = _lines(stream)[0]
    assert rec["strategy_id"] == "orb_options"
    assert rec["order_id"] == "PAPER-orb_options-abc"
    assert rec["qty"] == 75
    assert rec["side"] == "BUY"


def test_registered_secrets_are_masked_in_message_and_extras(stream: io.StringIO) -> None:
    DEFAULT_MASKER.register(FAKE_TOKEN)
    log = get_logger("t")
    log.info("using token %s", FAKE_TOKEN, extra={"detail": {"note": f"tok={FAKE_TOKEN}"}})
    raw = stream.getvalue()
    assert FAKE_TOKEN not in raw


def test_sensitive_extra_keys_are_masked(stream: io.StringIO) -> None:
    get_logger("t").info("headers", extra={"access_token": "unregistered-token-value", "api_key": "k"})
    rec = _lines(stream)[0]
    assert rec["access_token"] == "****"
    assert rec["api_key"] == "****"


def test_exception_tracebacks_are_masked(stream: io.StringIO) -> None:
    DEFAULT_MASKER.register(FAKE_TOKEN)
    try:
        raise RuntimeError(f"auth failed for Bearer {FAKE_TOKEN}")
    except RuntimeError:
        get_logger("t").exception("upstox call failed")
    raw = stream.getvalue()
    assert FAKE_TOKEN not in raw
    rec = _lines(stream)[0]
    assert "RuntimeError" in rec["exception"]


def test_file_handler_writes_jsonl(tmp_path: Path) -> None:
    configure_logging(level="INFO", mode=TradingMode.PAPER, console=False, log_dir=tmp_path, file_name="t.jsonl")
    get_logger("t").warning("to file")
    for h in logging.getLogger().handlers:
        h.flush()
    lines = (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["event"] == "to file"
    configure_logging(console=False)


def test_configure_is_idempotent() -> None:
    buf = io.StringIO()
    configure_logging(stream=buf)
    configure_logging(stream=buf)
    get_logger("t").info("once")
    assert len(_lines(buf)) == 1
    configure_logging(console=False)


def test_unserialisable_extras_do_not_crash(stream: io.StringIO) -> None:
    get_logger("t").info("obj", extra={"path": Path("x/y"), "obj": object()})
    rec = _lines(stream)[0]
    assert rec["path"] in ("x/y", "x\\y")
