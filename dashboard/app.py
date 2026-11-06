"""NIFTY options bot dashboard (Streamlit). UI only.

Run:   streamlit run dashboard/app.py            (uses database.path from config)
       streamlit run dashboard/app.py -- --db PATH

It listens only on 127.0.0.1 (``.streamlit/config.toml``) and refuses to run
on any other address unless started with ``-- --allow-remote`` (then put it
behind a TLS reverse proxy). It never creates or migrates the database: the
bot does; on a schema-version mismatch the dashboard refuses to run.

It reads the bot's published state and the SQLite history, and sends
commands by queueing them for the bot (``dashboard.commands``). It never
talks to the broker, and it holds no trading or risk logic. PAPER is the
default on every start and in every new session; LIVE needs the typed
confirmation AND the bot's approval, which this build always refuses.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import streamlit as st

from config.loader import load_config
from config.settings import BotConfig
from core.clock import now_ist
from dashboard.auth import LoginGate, bind_is_local, credentials_from, require_login
from dashboard.commands import CommandClient
from dashboard.components import charts, control_bar, learning, logs, overview, positions, risk_panel
from dashboard.data_access import (
    all_trades,
    commands_table,
    database_path,
    learning_candidates,
    model_status,
    open_repositories,
    orders_table,
    pending_approvals,
    performance,
    portfolio,
    risk_events_table,
    runtime_view,
    trades_on,
)
from data.storage.database import DatabaseError
from data.storage.repositories import Repositories

LIVE_TOGGLE = "live_toggle"


def _cli() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--db")
    parser.add_argument("--allow-remote", action="store_true")
    args, _ = parser.parse_known_args(sys.argv[1:])
    return args


def guard_network(allow_remote: bool) -> None:
    """Refuse to serve the controls on a non-loopback address by default."""
    address = st.get_option("server.address")
    if bind_is_local(address) or allow_remote:
        return
    shown = address or "all interfaces"
    st.error(f"Refusing to run: the dashboard is listening on {shown}. It must listen only on 127.0.0.1 "
             "(see .streamlit/config.toml, or pass --server.address 127.0.0.1). For remote access, put it "
             "behind a TLS reverse proxy and start it with `-- --allow-remote`.")
    st.stop()


@st.cache_resource
def _config() -> BotConfig:
    return load_config()


@st.cache_resource
def _repositories(path: str) -> Repositories:
    return open_repositories(_config(), Path(path))


def live(cfg: BotConfig, repos: Repositories, client: CommandClient) -> None:
    view = runtime_view(repos)
    strategy_ids = list(cfg.strategies.strategies)

    control_bar.mode_banner(st, view)
    if st.session_state.pop("reset_live_toggle", False):
        st.session_state[LIVE_TOGGLE] = False
    top = st.columns([1.1, 1.8, 1.6, 1.5])
    with top[0]:
        st.toggle("LIVE", key=LIVE_TOGGLE, help="PAPER is the default. LIVE needs the typed confirmation and "
                                               "every safety precondition; this build always refuses it.")
        st.caption(f"Mode: **{view.get('mode', default='PAPER')}**")
        flash = st.session_state.pop("live_flash", None)
        if flash:
            (st.success if flash[0] else st.error)(flash[1])
    with top[1]:
        control_bar.engine_controls(st, client, view)
    with top[2]:
        control_bar.kill_switch(st, client, view)
    with top[3]:
        control_bar.autonomy(st, client, view)
    control_bar.status_strip(st, view)
    control_bar.market_strip(st, view)
    control_bar.kill_banner_and_reset(st, client, view)
    if st.session_state.get(LIVE_TOGGLE):
        control_bar.live_panel(st, cfg, client, view)
    else:
        control_bar.last_live_request(st, view)

    trades = all_trades(repos, strategy_ids)
    perf = performance(trades)
    bot_time = view.get("bot_time")
    today = (now_ist() if not bot_time else now_ist().fromisoformat(bot_time)).date()
    tabs = st.tabs(["Overview", "Positions", "Risk", "Charts", "Logs", "Models & approvals"])
    with tabs[0]:
        overview.portfolio(st, portfolio(cfg, view, perf, len(trades_on(trades, today))))
        overview.strategy_cards(st, client, view)
    with tabs[1]:
        positions.render(st, client, view)
    risk_events = risk_events_table(repos)
    with tabs[2]:
        risk_panel.render(st, cfg, view, risk_events)
    with tabs[3]:
        charts.render(st, view, trades, perf, strategy_ids)
    with tabs[4]:
        logs.render(st, view, orders_table(repos), risk_events, commands_table(repos))
    with tabs[5]:
        learning.render(st, model_status(repos), learning_candidates(repos, strategy_ids), pending_approvals(repos))


def main() -> None:
    st.set_page_config(page_title="NIFTY Options Bot", page_icon="📈", layout="wide")
    cli = _cli()
    guard_network(bool(cli.allow_remote))
    cfg = _config()
    user = require_login(st, credentials_from(cfg.env.dashboard_username, cfg.env.dashboard_password))
    override: Any = st.session_state.get("db_override") or cli.db
    path = database_path(cfg, override)
    try:
        repos = _repositories(str(path))
        repos.db.verify_schema()  # every run: the bot may have been upgraded meanwhile
    except DatabaseError as exc:
        st.error(f"Refusing to start: {exc}")
        st.stop()
        raise  # pragma: no cover - st.stop() raises
    client = CommandClient(repos, user)
    st.session_state.setdefault(LIVE_TOGGLE, False)  # PAPER on every new session

    with st.sidebar:
        st.markdown(f"Signed in as **{user}**")
        if st.button("Sign out", key="logout"):
            LoginGate(st.session_state, None).logout()  # type: ignore[arg-type]
            st.rerun()
        auto = st.toggle("Auto-refresh", value=True, key="auto_refresh")
        every = st.select_slider("Refresh every (s)", [2, 3, 5, 10], value=3, key="refresh_every")
        st.caption(f"Database: `{path.name}`")
    st.fragment(run_every=every if auto else None)(live)(cfg, repos, client)


main()
