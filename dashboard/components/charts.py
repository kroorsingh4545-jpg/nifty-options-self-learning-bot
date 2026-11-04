"""Charts (Plotly): NIFTY intraday with overlays, cumulative P&L, P&L by
strategy, drawdown, trade distribution. One y-axis per chart; every chart
has a table view."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pandas as pd
import plotly.graph_objects as go

from dashboard.components.theme import layout, palette, strategy_color
from dashboard.data_access import (
    Performance,
    RuntimeView,
    candles_frame,
    ema_series,
    trade_markers,
    vwap_series,
)
from data.storage.records import TradeRecord

EMA_PERIOD = 20


def _end_label(fig: go.Figure, x: Any, y: Any, text: str, pal: dict[str, Any]) -> None:
    if y is None:
        return
    fig.add_annotation(x=x, y=y, text=text, showarrow=False, xanchor="left", xshift=6,
                       font={"color": pal["text"], "size": 11})


def _empty(fig: go.Figure, pal: dict[str, Any], text: str) -> go.Figure:
    fig.add_annotation(text=text, showarrow=False, x=0.5, y=0.5, xref="paper", yref="paper",
                       font={"color": pal["muted"], "size": 13})
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return fig


NO_TRADES = "No closed trades yet"


def intraday_figure(view: RuntimeView, trades: Sequence[TradeRecord], pal: dict[str, Any], *,
                    source: str, tf: int) -> tuple[go.Figure, pd.DataFrame]:
    keys = view.get("chart_keys", default={}) or {}
    key = keys.get("future") if source == "futures" else keys.get("index")
    df = candles_frame(view, key, tf)
    title = f"NIFTY {'futures' if source == 'futures' else 'index'} · {tf}-min"
    fig = go.Figure()
    fig.update_layout(**layout(pal, title=title, height=420, yaxis="price"))
    if df.empty:
        return _empty(fig, pal, "No completed candles yet"), df
    series = [("Price", df["close"].tolist())]
    df["ema"] = ema_series(df["close"].tolist(), EMA_PERIOD)
    if source == "futures":
        df["vwap"] = vwap_series(df)
        series += [("VWAP", df["vwap"].tolist())]
    series += [(f"EMA {EMA_PERIOD}", df["ema"].tolist())]
    for i, (name, ys) in enumerate(series):
        fig.add_trace(go.Scatter(x=df["start"], y=ys, name=name, mode="lines",
                                 line={"width": 2, "color": pal["series"][i]},
                                 hovertemplate=f"{name} %{{y:.2f}}<extra></extra>"))
        last = next((v for v in reversed(ys) if v is not None and v == v), None)
        _end_label(fig, df["start"].iloc[-1], last, name, pal)
    if source == "index":
        orb = next((s for s in (view.get("strategies", default=[]) or []) if s["strategy_id"] == "orb_options"),
                   None)
        rng = ((orb or {}).get("detail") or {}).get("opening_range") or {}
        for lvl, name in ((rng.get("high"), "ORB high"), (rng.get("low"), "ORB low")):
            if lvl is not None:
                fig.add_hline(y=lvl, line={"color": pal["muted"], "width": 1, "dash": "dash"},
                              annotation_text=f"{name} {lvl:.1f}", annotation_position="top right",
                              annotation_font={"color": pal["text"], "size": 11})
    marks = trade_markers(trades, df)
    for kind, symbol in (("entry", "triangle-up"), ("exit", "x")):
        m = marks[marks["kind"] == kind]
        if not m.empty:
            fig.add_trace(go.Scatter(x=m["time"], y=m["price"], mode="markers", name=kind.capitalize(),
                                     marker={"symbol": symbol, "size": 11, "color": pal["text"],
                                             "line": {"color": pal["surface"], "width": 2}},
                                     text=m["label"], hovertemplate="%{text}<extra></extra>"))
    return fig, df


def cumulative_figure(perf: Performance, pal: dict[str, Any]) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(**layout(pal, title="Cumulative net P&L (₹)", yaxis="₹"))
    eq = perf.equity
    if eq.empty:
        return _empty(fig, pal, NO_TRADES)
    fig.add_trace(go.Scatter(x=eq["time"], y=eq["cumulative"], mode="lines+markers", name="Cumulative",
                                 line={"width": 2, "color": pal["series"][0]}, marker={"size": 8},
                                 hovertemplate="₹%{y:,.2f}<extra></extra>", showlegend=False))
    return fig


def drawdown_figure(perf: Performance, pal: dict[str, Any]) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(**layout(pal, title="Drawdown from peak (₹)", yaxis="₹"))
    eq = perf.equity
    if eq.empty:
        return _empty(fig, pal, NO_TRADES)
    fig.add_trace(go.Scatter(x=eq["time"], y=eq["drawdown"], mode="lines", fill="tozeroy", name="Drawdown",
                                 line={"width": 2, "color": pal["series"][0]},
                                 hovertemplate="₹%{y:,.2f}<extra></extra>", showlegend=False))
    return fig


def strategy_pnl_figure(perf: Performance, strategy_ids: list[str], pal: dict[str, Any]) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(**layout(pal, title="Net P&L by strategy (₹)", yaxis="₹", hovermode="closest"),
                      bargap=0.35, barcornerradius=4)
    if not perf.by_strategy:
        return _empty(fig, pal, NO_TRADES)
    ids = [s for s in strategy_ids if s in perf.by_strategy]
    vals = [perf.by_strategy.get(s, 0.0) for s in ids]
    fig.add_trace(go.Bar(x=ids, y=vals, marker={"color": [strategy_color(pal, strategy_ids, s) for s in ids],
                                                "line": {"color": pal["surface"], "width": 2}},
                         text=[f"₹{v:,.0f}" for v in vals], textposition="outside",
                         textfont={"color": pal["text"]}, hovertemplate="%{x}: ₹%{y:,.2f}<extra></extra>",
                         showlegend=False))
    return fig


def distribution_figure(perf: Performance, pal: dict[str, Any]) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(**layout(pal, title="Trade outcome distribution (net ₹ per trade)", yaxis="trades",
                               hovermode="closest"), bargap=0.06, barcornerradius=4)
    eq = perf.equity
    if eq.empty:
        return _empty(fig, pal, NO_TRADES)
    fig.add_trace(go.Histogram(x=eq["net_pnl"], nbinsx=20, marker={"color": pal["series"][0],
                                   "line": {"color": pal["surface"], "width": 2}},
                                   hovertemplate="₹%{x}: %{y} trades<extra></extra>", showlegend=False))
    return fig


def render(st: Any, view: RuntimeView, trades: Sequence[TradeRecord], perf: Performance,
           strategy_ids: list[str]) -> None:
    pal = palette(st)
    st.subheader("Charts")
    c = st.columns([2, 1, 3])
    source = c[0].radio("Intraday chart", ["index", "futures"], horizontal=True, key="chart_source",
                        format_func=lambda s: "NIFTY index (ORB)" if s == "index" else "NIFTY futures (VWAP+EMA)")
    tf = c[1].selectbox("Timeframe", [1, 5], index=1, key="chart_tf", format_func=lambda m: f"{m} min")
    fig, df = intraday_figure(view, trades, pal, source=source, tf=tf)
    st.plotly_chart(fig, width="stretch", key="chart_intraday", theme=None)
    with st.expander("Intraday data table"):
        st.dataframe(df, hide_index=True, width="stretch", key="chart_intraday_table")
    a, b = st.columns(2)
    a.plotly_chart(cumulative_figure(perf, pal), width="stretch", key="chart_cum", theme=None)
    b.plotly_chart(strategy_pnl_figure(perf, strategy_ids, pal), width="stretch", key="chart_strat",
                   theme=None)
    a, b = st.columns(2)
    a.plotly_chart(drawdown_figure(perf, pal), width="stretch", key="chart_dd", theme=None)
    b.plotly_chart(distribution_figure(perf, pal), width="stretch", key="chart_dist", theme=None)
    with st.expander("P&L data table"):
        st.dataframe(perf.equity, hide_index=True, width="stretch", key="chart_pnl_table")
