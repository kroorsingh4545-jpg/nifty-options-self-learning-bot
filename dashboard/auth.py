"""Dashboard login and network exposure.

The dashboard listens only on 127.0.0.1 by default (``.streamlit/config.toml``)
and refuses to run on any other address unless started with
``--allow-remote`` (then put it behind a TLS reverse proxy).

Credentials come only from ``DASHBOARD_USERNAME`` / ``DASHBOARD_PASSWORD``
(``.env`` or environment, validated by ``config``). Without them the
dashboard refuses to run (fail closed). Comparison is constant-time; after
``MAX_FAILURES`` wrong attempts the session is locked for ``LOCKOUT_SECONDS``.
The password is never displayed, logged or stored in session state.
"""

from __future__ import annotations

import hmac
import time
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from typing import Any

from pydantic import SecretStr

MAX_FAILURES = 5
LOCKOUT_SECONDS = 300.0


LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def bind_is_local(address: str | None) -> bool:
    """True only for an explicit loopback address. Unset or empty means
    Streamlit listens on every interface, which is not local."""
    return bool(address) and str(address).strip().lower() in LOOPBACK


@dataclass(frozen=True)
class Credentials:
    username: str
    password: SecretStr


def credentials_from(username: str | None, password: SecretStr | None) -> Credentials | None:
    if not username or password is None:
        return None
    return Credentials(username, password)


def check_credentials(username: str, password: str, expected: Credentials) -> bool:
    user_ok = hmac.compare_digest(username.encode("utf-8"), expected.username.encode("utf-8"))
    pw_ok = hmac.compare_digest(password.encode("utf-8"), expected.password.get_secret_value().encode("utf-8"))
    return user_ok and pw_ok


class LoginGate:
    """Session-scoped login state machine (no Streamlit dependency)."""

    def __init__(self, session: MutableMapping[str, Any], expected: Credentials | None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.session = session
        self.expected = expected
        self._clock = clock

    @property
    def configured(self) -> bool:
        return self.expected is not None

    @property
    def user(self) -> str | None:
        return self.session.get("auth_user")

    def locked_for(self) -> float:
        until = float(self.session.get("auth_locked_until", 0.0))
        return max(0.0, until - self._clock())

    def attempt(self, username: str, password: str) -> tuple[bool, str]:
        if self.expected is None:
            return False, "Dashboard login is not configured: set DASHBOARD_USERNAME and DASHBOARD_PASSWORD."
        if self.locked_for() > 0:
            return False, f"Too many failed attempts. Try again in {int(self.locked_for()) + 1} s."
        if check_credentials(username, password, self.expected):
            self.session["auth_user"] = self.expected.username
            self.session["auth_failures"] = 0
            return True, "Signed in."
        failures = int(self.session.get("auth_failures", 0)) + 1
        self.session["auth_failures"] = failures
        if failures >= MAX_FAILURES:
            self.session["auth_locked_until"] = self._clock() + LOCKOUT_SECONDS
            self.session["auth_failures"] = 0
            return False, f"Too many failed attempts. Locked for {int(LOCKOUT_SECONDS)} s."
        return False, "Wrong username or password."

    def logout(self) -> None:
        self.session.pop("auth_user", None)


def require_login(st: Any, expected: Credentials | None) -> str:
    """Render the login form until the user is signed in; returns the username."""
    gate = LoginGate(st.session_state, expected)
    if not gate.configured:
        st.error("Dashboard login is not configured. Set DASHBOARD_USERNAME and DASHBOARD_PASSWORD "
                 "(at least 8 characters) in .env, then restart the dashboard.")
        st.stop()
    if gate.user:
        return str(gate.user)
    st.title("NIFTY Options Bot")
    st.caption("Sign in to continue. The bot runs in PAPER mode.")
    with st.form("login", clear_on_submit=True):
        username = st.text_input("Username", key="login_user")
        password = st.text_input("Password", type="password", key="login_password")
        submitted = st.form_submit_button("Sign in")
    if submitted:
        ok, msg = gate.attempt(username, password)
        st.session_state.pop("login_password", None)
        if ok:
            st.rerun()
        st.error(msg)
    st.stop()
    raise RuntimeError("unreachable")  # pragma: no cover - st.stop() raises
