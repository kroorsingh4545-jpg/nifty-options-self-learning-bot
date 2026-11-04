"""Logs: live event stream, order log, risk events, alerts, commands."""

from __future__ import annotations

from typing import Any

import pandas as pd

from dashboard.data_access import RuntimeView


def _event_line(e: dict[str, Any]) -> dict[str, Any]:
    p = e.get("payload") or {}
    if e["topic"] == "entry_decision":
        what = (f"{p.get('strategy_id')} {'APPROVED' if p.get('approved') else 'rejected'}"
                + ("" if p.get("approved") else f": {'; '.join(p.get('reasons') or [])}"))
    elif e["topic"] == "signal_filtered":
        sig = p.get("signal") or {}
        what = f"{sig.get('strategy_id')} filtered: {p.get('reason')}"
    else:
        what = str(p)[:200]
    return {"time": e["at"][11:19], "event": e["topic"], "detail": what}


def render(st: Any, view: RuntimeView, orders: pd.DataFrame, risk_events: pd.DataFrame,
           commands: pd.DataFrame) -> None:
    st.subheader("Logs")
    channels = view.get("alert_channels", default={}) or {}
    st.caption("Alert channels: " + " · ".join(f"{k}: {v}" for k, v in channels.items()) if channels
               else "Alert channels: unknown (bot offline)")
    t1, t2, t3, t4, t5 = st.tabs(["Live events", "Alerts", "Orders", "Risk events", "Operator commands"])
    with t1:
        events = [_event_line(e) for e in (view.get("events", default=[]) or [])]
        st.dataframe(pd.DataFrame(events, columns=["time", "event", "detail"]), hide_index=True,
                     width="stretch", key="log_events")
    with t2:
        st.dataframe(pd.DataFrame(view.get("alerts", default=[]) or [],
                                  columns=["at", "severity", "title", "message", "strategy_id"]),
                     hide_index=True, width="stretch", key="log_alerts")
    with t3:
        st.dataframe(orders, hide_index=True, width="stretch", key="log_orders")
    with t4:
        st.dataframe(risk_events, hide_index=True, width="stretch", key="log_risk")
    with t5:
        st.dataframe(commands, hide_index=True, width="stretch", key="log_commands")
