"""Portfolio overview and strategy cards."""

from __future__ import annotations

from typing import Any

from dashboard.commands import CommandClient
from dashboard.data_access import RuntimeView, inr, pct, ratio


def portfolio(st: Any, p: dict[str, Any]) -> None:
    st.subheader("Portfolio")
    r1 = st.columns(5)
    r1[0].metric("Total capital", inr(p["total_capital"]))
    r1[1].metric("Deployed capital", inr(p["deployed_capital"]))
    r1[2].metric("Available funds", inr(p["available_funds"]))
    r1[3].metric("Open positions", p["open_positions"])
    r1[4].metric("Trades today", p["trades_today"])
    r2 = st.columns(5)
    r2[0].metric("Realized P&L", inr(p["realized_pnl"], 2))
    r2[1].metric("Unrealized P&L", inr(p["unrealized_pnl"], 2))
    r2[2].metric("Total P&L", inr(p["total_pnl"], 2))
    r2[3].metric("Daily loss used", inr(p["daily_loss_used"], 2), f"limit {inr(p['daily_loss_limit'])}",
                 delta_color="off", delta_arrow="off")
    r2[4].metric("Daily loss remaining", inr(p["daily_loss_remaining"], 2))
    r3 = st.columns(5)
    r3[0].metric("Win rate", pct(p["win_rate"]))
    r3[1].metric("Profit factor", ratio(p["profit_factor"]))
    r3[2].metric("Max drawdown", inr(p["max_drawdown"], 2))


def _status_label(status: str | None) -> str:
    return {"ACTIVE": "🟢 ACTIVE", "PAUSED": "🟡 PAUSED", "KILLED": "🔴 KILLED",
            "DISABLED": "⚪ DISABLED"}.get(status or "", status or "—")


def strategy_cards(st: Any, client: CommandClient, view: RuntimeView) -> None:
    st.subheader("Strategies")
    strategies = view.get("strategies", default=[]) or []
    if not strategies:
        st.info("No strategy state yet: start the bot to see live strategy cards.")
        return
    cols = st.columns(len(strategies))
    for col, s in zip(cols, strategies, strict=True):
        sid = s["strategy_id"]
        with col, st.container(border=True):
            st.markdown(f"**{s['name']}** · `{sid}` · v{s['version']}")
            st.markdown(f"{_status_label(s.get('status'))} · {'enabled' if s['enabled'] else 'disabled'} in "
                        f"config · mode {s['mode']}")
            a = st.columns(3)
            a[0].metric("Allocated", inr(s["allocated_capital"]))
            a[1].metric("P&L today", inr(s["realized"] + s["unrealized"], 2))
            limit = s.get("daily_loss_limit") or 0
            a[2].metric("Daily loss used", inr(s["daily_loss"], 2), f"of {inr(limit)}", delta_color="off",
                        delta_arrow="off")
            b = st.columns(3)
            b[0].metric("Open trades", s["open_trades"])
            b[1].metric("Trades today", s["trades_today"])
            b[2].metric("Risk / trade", inr(s.get("max_risk_per_trade")))
            sig = s.get("last_signal")
            st.caption("Last signal: " + (f"{sig['option_type']} · {sig['reason']} · {sig['generated_at'][11:19]}"
                                          if sig else "none today"))
            if s.get("last_decision"):
                st.caption(f"Last decision: {s['last_decision']}")
            ml = s.get("ml_score")
            ml_text = f"{ml:.2f}" if ml is not None else "— (no model until Phase 7)"
            st.caption(f"ML confidence: {ml_text} · "
                       f"Regime: {s.get('regime') or '— (Phase 7)'} · Model: {s.get('model_version') or 'none'}")
            if s.get("kill"):
                st.error(s["kill"])
            if s.get("paused"):
                st.warning(f"Paused: {s['paused']}")
            key = f"confirm_kill_{sid}"
            if st.button(f"Kill {sid}", key=f"kill_{sid}", width="stretch",
                         help="Strategy kill switch: blocks this strategy and exits its positions."):
                st.session_state[key] = True
            if st.session_state.get(key):
                st.warning(f"Kill {sid}? Its positions are exited; other strategies are unaffected.")
                c1, c2 = st.columns(2)
                if c1.button("Confirm", key=f"kill_ok_{sid}", type="primary", width="stretch"):
                    res = client.kill(note=f"strategy kill from dashboard ({sid})", strategy_id=sid)
                    (st.success if res.ok else st.error)(res.message)
                    st.session_state[key] = False
                if c2.button("Cancel", key=f"kill_no_{sid}", width="stretch"):
                    st.session_state[key] = False
                    st.rerun()
