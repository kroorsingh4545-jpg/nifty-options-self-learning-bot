"""Risk panel: limits, exposure, margin, API usage, subscriptions, events."""

from __future__ import annotations

from typing import Any

import pandas as pd

from config.settings import BotConfig
from dashboard.data_access import RuntimeView, inr


def render(st: Any, cfg: BotConfig, view: RuntimeView, risk_events: pd.DataFrame) -> None:
    st.subheader("Risk")
    daily = view.get("daily", default={}) or {}
    r = st.columns(4)
    r[0].metric("Global daily loss limit", inr(cfg.risk.global_.max_daily_loss),
                f"used {inr(daily.get('global_loss', 0.0), 2)}", delta_color="off", delta_arrow="off")
    r[1].metric("Per-strategy daily loss limit", inr(cfg.risk.per_strategy.max_daily_loss))
    r[2].metric("Per-trade risk", inr(cfg.risk.per_trade.max_risk_per_trade))
    r[3].metric("Open exposure", inr(view.get("capital", "deployed", default=0.0)))
    funds = view.get("funds") or {}
    used, avail = funds.get("used_margin"), funds.get("available_margin")
    m = st.columns(4)
    usage = (used / (used + avail)) if used is not None and avail and (used + avail) > 0 else None
    m[0].metric("Margin usage", f"{usage * 100:.1f}%" if usage is not None else "—",
                f"used {inr(used)}" if used is not None else "", delta_color="off", delta_arrow="off")
    m[1].metric("WebSocket subscriptions", view.get("connections", "subscriptions", default=0))
    limits = view.get("rate_limits")
    if limits:
        parts = []
        for cat, u in limits.items():
            worst = max((w.get("pct", 0.0) for w in (u.get("windows") or {}).values()), default=0.0)
            parts.append(f"{cat}: {worst:.0f}%")
        m[2].metric("API rate-limit usage", " · ".join(parts))
    else:
        m[2].metric("API rate-limit usage", "n/a", "no Upstox client in this session", delta_color="off",
                    delta_arrow="off")
    rec = view.get("reconciliation")
    m[3].metric("Reconciliation", "—" if rec is None else ("OK" if rec["ok"] else "MISMATCH"),
                "" if rec is None else f"{rec['trigger']} {rec['at'][11:19]}", delta_color="off", delta_arrow="off")
    if rec and not rec["ok"]:
        st.error("Reconciliation mismatch: " + "; ".join(rec.get("mismatches") or ["broker unreachable"]))

    per = []
    for sid, s in (daily.get("strategies") or {}).items():
        per.append({"Strategy": sid, "Loss today": s.get("loss", 0.0), "Limit": s.get("limit"),
                    "Trades": s.get("trades", 0), "Consecutive losses": s.get("consecutive_losses", 0),
                    "Breached": "yes" if s.get("breached") else "no", "Paused": s.get("paused") or ""})
    st.dataframe(pd.DataFrame(per), hide_index=True, width="stretch", key="risk_per_strategy")
    st.markdown("**Recent risk events**")
    st.dataframe(risk_events.head(30), hide_index=True, width="stretch", key="risk_events")
