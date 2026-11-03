"""Credential masking used by logging, errors, and (later) the dashboard.

Two layers:
1. Exact values registered at startup (API key, secret, access token, ...)
   are replaced wherever they appear.
2. Pattern rules catch secrets that were never registered, e.g.
   ``Authorization: Bearer <token>`` or ``access_token=<value>``.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable
from typing import Any

MASK = "****"
_MIN_SECRET_LEN = 4

SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "api_secret",
        "apikey",
        "authorization",
        "client_secret",
        "password",
        "refresh_token",
        "secret",
        "token",
        "upstox_api_key",
        "upstox_api_secret",
        "upstox_access_token",
        "telegram_bot_token",
        "dashboard_password",
    }
)

_KEY_WORDS = (
    r"[a-z_]*access_token|refresh_token|api_key|api_secret|apikey|"
    r"client_secret|password|secret|token|authorization"
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization: Bearer <token>
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9\-._~+/]+=*"), r"\1" + MASK),
    # key=value / "key": "value" / key: value
    (
        re.compile(r"(?i)\b((?:" + _KEY_WORDS + r")[\"']?\s*[:=]\s*[\"']?)([^\"'\s,&}\]]+)"),
        r"\1" + MASK,
    ),
    # Telegram bot tokens: <digits>:<long token>
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), MASK),
)


def mask_secret(value: str | None) -> str:
    """Partially mask a secret for display, e.g. ``abcd****wxyz``.

    Values shorter than 16 characters are fully masked so the visible part
    never reveals a meaningful fraction of the secret.
    """
    if not value:
        return ""
    if len(value) < 16:
        return MASK
    return f"{value[:4]}{MASK}{value[-4:]}"


class SecretMasker:
    """Thread-safe registry of secret values plus pattern-based masking."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self._lock = threading.Lock()

    def register(self, *values: str | None) -> None:
        with self._lock:
            for value in values:
                if value and len(value) >= _MIN_SECRET_LEN:
                    self._secrets.add(value)

    def register_many(self, values: Iterable[str | None]) -> None:
        self.register(*values)

    def clear(self) -> None:
        with self._lock:
            self._secrets.clear()

    def mask_text(self, text: str) -> str:
        with self._lock:
            # Longest first so a secret containing another is fully replaced.
            secrets = sorted(self._secrets, key=len, reverse=True)
        for secret in secrets:
            if secret in text:
                text = text.replace(secret, mask_secret(secret))
        for pattern, repl in _PATTERNS:
            text = pattern.sub(repl, text)
        return text

    def mask_obj(self, obj: Any) -> Any:
        """Recursively mask strings in dicts/lists/tuples.

        Values under sensitive keys are fully masked regardless of content.
        """
        if isinstance(obj, str):
            return self.mask_text(obj)
        if isinstance(obj, dict):
            out: dict[Any, Any] = {}
            for key, val in obj.items():
                if isinstance(key, str) and key.lower() in SENSITIVE_KEYS and val:
                    out[key] = MASK
                else:
                    out[key] = self.mask_obj(val)
            return out
        if isinstance(obj, (list, tuple)):
            return type(obj)(self.mask_obj(v) for v in obj)
        return obj


#: Process-wide masker. Secrets loaded by ``config`` are registered here.
DEFAULT_MASKER = SecretMasker()
