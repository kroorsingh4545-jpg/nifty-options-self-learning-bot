"""Point-in-time variance bias by VIX-change state (the D4 kept hypothesis), pure.

``bias(day, state)``: the realised / VIX-implied variance ratio of 30-minute
windows (D4 checkpoint samples, outcome ``log_rv_implied``) whose VIX-change
state is ``state``, over the ``lookback`` sessions BEFORE ``day``, as
``exp(mean log ratio)``. Fewer than ``min_windows`` in the state: all states
pooled ("POOLED"); fewer than that in total: None.
"""

from __future__ import annotations

import bisect
import math
from collections import defaultdict
from collections.abc import Sequence
from datetime import date

from research.regime.samples import Sample


class VarianceBias:
    def __init__(self, samples: Sequence[Sample], *, horizon: int, lookback: int, min_windows: int) -> None:
        rows: dict[date, list[tuple[str, float]]] = defaultdict(list)
        for s in samples:
            v = s.outcomes.get("log_rv_implied")
            if s.kind == "CHECKPOINT" and v is not None and math.isfinite(v) \
                    and (s.t1 - s.t0).total_seconds() == horizon * 60:
                rows[s.day].append((str(s.regime.get("vol_change")), float(v)))
        self.days = sorted(rows)
        self.rows = rows
        self.lookback = lookback
        self.min_windows = min_windows

    def __call__(self, day: date, state: str | None) -> tuple[float | None, str | None, int]:
        i = bisect.bisect_left(self.days, day)
        window = self.days[max(0, i - self.lookback):i]
        vals = [(st, v) for d in window for st, v in self.rows[d]]
        same = [v for st, v in vals if st == state]
        if state is not None and len(same) >= self.min_windows:
            return math.exp(sum(same) / len(same)), "STATE", len(same)
        if len(vals) >= self.min_windows:
            return math.exp(sum(v for _, v in vals) / len(vals)), "POOLED", len(vals)
        return None, None, len(vals)


__all__ = ["VarianceBias"]
