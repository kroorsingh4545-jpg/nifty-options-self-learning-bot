"""Mode banner and control bar: PAPER/LIVE toggle, Start/Pause/Stop,
emergency kill switch, connection / session status, square-off countdown,
autonomy level, and the named kill-switch reset."""

from __future__ import annotations

from typing import Any

from config.settings import BotConfig
from core.enums import AutonomyLevel
from core.mode_controller import LIVE_CONFIRMATION_PHRASE
from dashboard.commands import CommandClient, Submitted
from dashboard.data_access import (
    RuntimeView,
    clock_status,
    duration,
    entry_block,
    feed_lag_status,
    inr,
    price_status,
    websocket_status,
)

KILL_CONFIRM = "confirm_kill"


def _flash(st: Any, res: Submitted) -> None:
    (st.success if res.ok else st.error)(res.message)


def mode_banner(st: Any, view: RuntimeView) -> None:
    mode = view.get("mode", default="PAPER")
    if mode == "LIVE":  # pragma: no cover - LIVE cannot be enabled in this build
        st.error("🔴 LIVE MODE: REAL ORDERS")
        return
    st.markdown(
        "<div data-testid='mode-banner' style='background:#0b5cad;color:#fff;padding:10px 16px;border-radius:6px;"
        "font-size:20px;font-weight:700;letter-spacing:.5px'>🛡️ PAPER MODE · simulated orders only · "
        "no real money</div>", unsafe_allow_html=True)


def kill_switch(st: Any, client: CommandClient, view: RuntimeView) -> None:
    """Always visible: one click, then confirm."""
    active = view.get("kill_switch", "global_active", default=False)
    label = "🛑 KILL SWITCH ACTIVE" if active else "🛑 EMERGENCY KILL SWITCH"
    if st.button(label, key="kill_btn", type="primary", width="stretch",
                 help="Blocks new orders, cancels pending orders and exits every position."):
        st.session_state[KILL_CONFIRM] = True
    if st.session_state.get(KILL_CONFIRM):
        with st.container(border=True):
            st.warning("Confirm the emergency stop: new orders are blocked, pending orders cancelled and ALL "
                       "positions exited through the execution layer. Only a named reset re-enables trading.")
            note = st.text_input("Reason (optional)", key="kill_note")
            c1, c2 = st.columns(2)
            if c1.button("Confirm kill switch", key="kill_confirm", type="primary", width="stretch"):
                _flash(st, client.kill(note=note))
                st.session_state[KILL_CONFIRM] = False
            if c2.button("Cancel", key="kill_cancel", width="stretch"):
                st.session_state[KILL_CONFIRM] = False
                st.rerun()


def engine_controls(st: Any, client: CommandClient, view: RuntimeView) -> None:
    state = view.get("engine_state", default="OFFLINE")
    st.caption(f"Engine: **{state}**")
    c1, c2, c3 = st.columns(3)
    if c1.button("▶ Start", key="eng_start", width="stretch"):
        _flash(st, client.engine("start"))
    if c2.button("⏸ Pause", key="eng_pause", width="stretch"):
        _flash(st, client.engine("pause"))
    if c3.button("⏹ Stop", key="eng_stop", width="stretch",
                 help="Stops NEW entries only. Stops, square-off and reconciliation keep running."):
        _flash(st, client.engine("stop"))


def market_strip(st: Any, view: RuntimeView) -> None:
    """Latest NIFTY index / future prices from the live feed, the feed lag and
    the PC clock offset."""
    cols = st.columns(4)
    quiet = {"delta_color": "off", "delta_arrow": "off"}
    for col, (label, which) in zip(cols, (("NIFTY index", "index"), ("NIFTY future", "future")), strict=False):
        value, detail = price_status(view, which)
        col.metric(label, value, detail, **quiet)
    lag, lag_detail, too_high = feed_lag_status(view)
    cols[2].metric("Feed lag", lag, lag_detail, **quiet)
    offset, clock_detail, drifted = clock_status(view)
    cols[3].metric("PC clock", offset, clock_detail, **quiet)
    if not view.online:
        return
    if too_high:
        entry_limit = view.get("connections", "entry_feed_lag_limit_seconds")
        gate = f" Above ±{entry_limit:g}s new entries are blocked (exits are not)." if entry_limit else ""
        st.warning(f"Market-data feed lag is {lag} ({lag_detail}). Candles are timed by the exchange clock "
                   f"and the session by this PC's clock: sync the PC clock (Windows: `w32tm /resync`).{gate}")
    if drifted:
        st.warning(f"PC clock has drifted {offset} from Upstox server time ({clock_detail}). Sync it "
                   "(Windows, admin: `net start w32time` then `w32tm /resync`).")


def status_strip(st: Any, view: RuntimeView) -> None:
    cols = st.columns(6)
    online = view.online
    age = view.age_seconds
    quiet = {"delta_color": "off", "delta_arrow": "off"}
    cols[0].metric("Bot", "ONLINE" if online else "OFFLINE",
                   f"heartbeat {age:.0f}s ago" if age is not None else "no heartbeat", **quiet)
    cols[1].metric("Upstox", "connected" if view.get("connections", "upstox_connected") else "offline",
                   str(view.get("connections", "upstox", default="unknown")), **quiet)
    ws_value, ws_detail = websocket_status(view)
    cols[2].metric("WebSocket", ws_value, ws_detail, **quiet)
    phase = view.get("session", "phase", default="—")
    trading_day = view.get("session", "trading_day")
    cols[3].metric("Market session", phase, "trading day" if trading_day else "not a trading day", **quiet)
    secs = view.get("session", "seconds_to_square_off")
    cols[4].metric("Square-off in", duration(secs) if secs else "now / passed",
                   f"window from {view.get('session', 'square_off_start', default='15:10')}", **quiet)
    level = str(view.get("autonomy", default="—"))
    short = {"OBSERVE_ONLY": "Observe", "PAPER_AUTO_IMPROVE": "Paper auto",
             "LIVE_CANDIDATE_APPROVAL": "Live approval"}.get(level, level)
    cols[5].metric("Autonomy", short, level, **quiet)
    if not online:
        st.warning("The bot is not publishing state (offline or stopped). Commands are queued: a kill switch "
                   "is applied when the bot starts; other commands expire after 60 s.")
    block = entry_block(view)
    if block:
        st.error(f"New entries blocked: {block}")
    if view.state is not None and not view.get("session", "holiday_calendar_loaded", default=True):
        st.error("No NSE holiday calendar for this year: every new entry is blocked. Add it to config.")


def autonomy(st: Any, client: CommandClient, view: RuntimeView) -> None:
    levels = [a.value for a in AutonomyLevel]
    current = view.get("autonomy", default=AutonomyLevel.PAPER_AUTO_IMPROVE.value)
    with st.form("autonomy_form", border=False):
        level = st.selectbox("Autonomy level", levels, index=levels.index(current) if current in levels else 1,
                             key="autonomy_level")
        if st.form_submit_button("Apply autonomy") and level != current:
            _flash(st, client.set_autonomy(level))


def kill_banner_and_reset(st: Any, client: CommandClient, view: RuntimeView) -> None:
    events = view.get("kill_switch", "events", default=[]) or []
    if not events:
        return
    for ev in events:
        kind = "until a named human resets it" if ev.get("persistent") else "for the rest of the trading day"
        st.error(f"🛑 Kill switch active · {ev['scope']} · {ev['reason']} since {ev['since'][:16]} "
                 f"(active {kind}): {ev['message']}")
    with st.expander("Reset the kill switch (named operator)", expanded=False):
        st.caption(f"Signed in as **{client.operator}**: the reset is recorded under this name. The bot first "
                   "reconciles with the broker and refuses the reset on any mismatch. Daily-loss kills cannot "
                   "be reset on the same day. After the reset, each strategy that is no longer blocked is "
                   "re-activated explicitly.")
        scopes = [ev["scope"] for ev in events]
        with st.form("reset_form", clear_on_submit=True):
            scope = st.selectbox("Scope", scopes, key="reset_scope")
            note = st.text_input("What was checked / fixed (required)", key="reset_note")
            if st.form_submit_button("Reset kill switch"):
                _flash(st, client.reset_kill(note, None if scope == "GLOBAL" else scope))


def live_panel(st: Any, cfg: BotConfig, client: CommandClient, view: RuntimeView) -> None:
    """Shown when the LIVE toggle is switched on. The backend decides; in this
    build it always refuses (no LIVE order routing)."""
    with st.container(border=True):
        st.subheader("Switch to LIVE: preconditions")
        cols = st.columns(3)
        cols[0].metric("Broker connection", "connected" if view.get("connections", "upstox_connected")
                       else "not connected")
        cols[1].metric("Capital", inr(cfg.risk.capital.total_capital))
        active = [s["strategy_id"] for s in (view.get("strategies", default=[]) or []) if s.get("status") == "ACTIVE"]
        cols[2].metric("Active strategies", len(active), ", ".join(active) or "none", delta_color="off",
                       delta_arrow="off")
        cols = st.columns(3)
        cols[0].metric("Open positions", len(view.get("positions", default=[]) or []))
        ks = "ACTIVE" if view.get("kill_switch", "global_active") else "armed"
        cols[1].metric("Risk state", f"kill switch {ks}",
                       f"daily loss {inr(view.get('daily', 'global_loss', default=0.0))} of "
                       f"{inr(view.get('daily', 'global_limit', default=0.0))}", delta_color="off", delta_arrow="off")
        models = {s.get("model_version") for s in (view.get("strategies", default=[]) or [])}
        cols[2].metric("Model version", ", ".join(sorted(m for m in models if m)) or "none (rule-based)")
        pending = view.get("live_preconditions", "pending_orders", default=0) or 0
        unmet = view.get("live_preconditions", "unmet", default=[]) or []
        if unmet:
            st.error("LIVE will be refused. Unmet preconditions:\n\n" + "\n".join(f"- {r}" for r in unmet))
        if pending:
            st.warning(f"{pending} pending order(s): the mode cannot change until they are reconciled.")
        with st.form("live_form", clear_on_submit=True):
            phrase = st.text_input(f"Type exactly: {LIVE_CONFIRMATION_PHRASE}", key="live_phrase")
            approver = st.text_input("Approved by (named human)", key="live_approver")
            submitted = st.form_submit_button("Request LIVE", disabled=bool(pending))
        if submitted:
            res = client.request_live(phrase, approver)
            # The mode stays PAPER until the bot approves: switch the toggle back
            # immediately and show the outcome next to it.
            st.session_state["live_flash"] = (res.ok, res.message)
            st.session_state["reset_live_toggle"] = True
            st.rerun()
        last_live_request(st, view)


def last_live_request(st: Any, view: RuntimeView) -> None:
    last = view.get("last_live_request")
    if last and not last.get("approved"):
        st.info(f"Last LIVE request ({last.get('operator')}, {str(last.get('at', ''))[11:19]}) refused by the "
                "bot:\n\n" + "\n".join(f"- {r}" for r in last["reasons"]))
