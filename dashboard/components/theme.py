"""Chart colours and layout (validated with the dataviz palette validator).

Categorical slots 1-3 (blue, orange, aqua) pass every check all-pairs in both
modes. Aqua is below 3:1 on the light surface, so every chart that uses it
also has a legend, direct line-end labels and a table view (relief rule).
Strategies take slots in config order, so a colour always follows its
strategy. Status colours are reserved for state and always ship with text.
"""

from __future__ import annotations

from typing import Any

LIGHT = {
    "surface": "#fcfcfb", "text": "#0b0b0b", "muted": "#52514e", "grid": "#e4e3df",
    "series": ["#2a78d6", "#eb6834", "#1baf7a"],
}
DARK = {
    "surface": "#1a1a19", "text": "#f0efec", "muted": "#c3c2b7", "grid": "#383835",
    "series": ["#3987e5", "#d95926", "#199e70"],
}
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}


def palette(st: Any) -> dict[str, Any]:
    try:
        kind = st.context.theme.type
    except Exception:
        kind = None
    return DARK if kind == "dark" else LIGHT


def layout(pal: dict[str, Any], *, title: str, height: int = 320, yaxis: str = "",
           hovermode: str = "x unified") -> dict[str, Any]:
    return {
        "title": {"text": title, "font": {"size": 15, "color": pal["text"]}, "x": 0},
        "height": height,
        "margin": {"l": 56, "r": 96, "t": 44, "b": 40},
        "paper_bgcolor": pal["surface"], "plot_bgcolor": pal["surface"],
        "font": {"color": pal["muted"], "size": 12},
        "xaxis": {"gridcolor": pal["grid"], "zeroline": False, "showline": False},
        "yaxis": {"gridcolor": pal["grid"], "zeroline": False, "title": yaxis},
        "legend": {"orientation": "h", "y": 1.02, "x": 1, "xanchor": "right", "yanchor": "bottom"},
        "hovermode": hovermode,
    }


def strategy_color(pal: dict[str, Any], strategy_ids: list[str], sid: str) -> str:
    idx = strategy_ids.index(sid) if sid in strategy_ids else len(pal["series"]) - 1
    return str(pal["series"][min(idx, len(pal["series"]) - 1)])
