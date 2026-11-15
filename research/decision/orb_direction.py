"""How often historical ORB signals moved in their own direction (pure).

From the D4 history samples of kind ORB (every signal the strategy's rule would
have given on the stored 1-minute NIFTY history, each assumed taken): the share
of signals whose NIFTY move over the next ``h`` minutes was in the signal's
direction, with a Wilson 95 % interval, overall, by year and by direction, plus
the mean move in basis points and in trailing-sd units. A coin flip is 50 %.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from research.regime.samples import Sample
from volatility.calibration import wilson_interval


def _stats(samples: Sequence[Sample]) -> dict[str, Any]:
    moves = [s.outcomes["fwd_ret"] * s.direction for s in samples if math.isfinite(s.outcomes["fwd_ret"])]
    n, hits = len(moves), sum(1 for m in moves if m > 0)
    flat = sum(1 for m in moves if m == 0)
    lo, hi = wilson_interval(hits, n)
    cont = [s.outcomes["cont"] for s in samples if math.isfinite(s.outcomes.get("cont", math.nan))]
    return {"n": n, "own_direction": hits, "flat": flat, "share": None if not n else round(hits / n, 4),
            "ci95": [None if math.isnan(lo) else round(lo, 4), None if math.isnan(hi) else round(hi, 4)],
            "mean_move_bps": None if not n else round(1e4 * sum(moves) / n, 2),
            "mean_move_sd": None if not cont else round(sum(cont) / len(cont), 3)}


def orb_direction(by_horizon: Mapping[int, Iterable[Sample]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for h, samples in sorted(by_horizon.items()):
        orb = [s for s in samples if s.kind == "ORB"]
        years = sorted({s.day.year for s in orb})
        out[str(h)] = {"all": _stats(orb),
                       "by_year": {str(y): _stats([s for s in orb if s.day.year == y]) for y in years},
                       "by_direction": {d: _stats([s for s in orb if s.info.get("option_type") == d])
                                        for d in ("CE", "PE")}}
    return out


def render_text(rep: Mapping[str, Any], *, first: str, last: str) -> list[str]:
    lines = [f"Historical ORB signals ({first} .. {last}, every signal assumed taken): share whose NIFTY move "
             "over the next h minutes was in the signal's direction (coin flip = 50 %)"]
    for h, r in rep.items():
        a = r["all"]
        lines.append(f"  {h} min: {a['own_direction']} of {a['n']} = {100 * (a['share'] or 0):.1f} % "
                     f"(95 % CI {100 * (a['ci95'][0] or 0):.1f}-{100 * (a['ci95'][1] or 0):.1f} %), mean move "
                     f"{a['mean_move_bps']} bps in the signal's direction ({a['mean_move_sd']} trailing sd)")
        lines.append("     by year: " + ", ".join(f"{y} {100 * (s['share'] or 0):.1f} % (n {s['n']})"
                                                   for y, s in r["by_year"].items()))
        lines.append("     by direction: " + ", ".join(f"{d} {100 * (s['share'] or 0):.1f} % (n {s['n']})"
                                                        for d, s in r["by_direction"].items()))
    return lines


__all__ = ["orb_direction", "render_text"]
