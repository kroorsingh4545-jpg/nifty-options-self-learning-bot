"""Live positions table with a manual exit (plus confirmation) per position."""

from __future__ import annotations

from typing import Any

import pandas as pd

from dashboard.commands import CommandClient
from dashboard.data_access import RuntimeView, duration


def table(view: RuntimeView) -> pd.DataFrame:
    rows = []
    for p in view.get("positions", default=[]) or []:
        trail = p.get("trailing_stop") if p.get("trailing_active") else None
        rows.append({
            "Strategy": p["strategy_id"], "Symbol": p["symbol"], "Side": p["side"], "Quantity": p["quantity"],
            "Entry price": p["entry_price"], "LTP": p.get("ltp"), "Unrealized P&L": p["unrealized"],
            "Stop-loss": p["stop_loss"], "Target": p.get("target"),
            "Trailing stop": trail if trail is not None else "not active",
            "R-multiple": p.get("r_multiple"), "Holding time": duration(p.get("holding_seconds")),
        })
    return pd.DataFrame(rows, columns=["Strategy", "Symbol", "Side", "Quantity", "Entry price", "LTP",
                                       "Unrealized P&L", "Stop-loss", "Target", "Trailing stop", "R-multiple",
                                       "Holding time"])


def render(st: Any, client: CommandClient, view: RuntimeView) -> None:
    st.subheader("Live positions")
    df = table(view)
    st.dataframe(df, hide_index=True, width="stretch", key="positions_table")
    positions = view.get("positions", default=[]) or []
    if not positions:
        st.caption("No open positions.")
        return
    for p in positions:
        uid = p["trade_uid"]
        key = f"confirm_exit_{uid}"
        c = st.columns([4, 1])
        c[0].markdown(f"`{p['strategy_id']}` · **{p['symbol']}** · {p['quantity']} @ {p['entry_price']}"
                      + (" · exit in progress" if p.get("exit_in_progress") else ""))
        if c[1].button("Exit", key=f"exit_{uid}", width="stretch"):
            st.session_state[key] = True
        if st.session_state.get(key):
            st.warning(f"Exit {p['symbol']} ({p['quantity']}) at market now?")
            a, b = st.columns(2)
            if a.button("Confirm exit", key=f"exit_ok_{uid}", type="primary", width="stretch"):
                res = client.exit_position(uid, note="manual exit from dashboard")
                (st.success if res.ok else st.error)(res.message)
                st.session_state[key] = False
            if b.button("Cancel", key=f"exit_no_{uid}", width="stretch"):
                st.session_state[key] = False
                st.rerun()
