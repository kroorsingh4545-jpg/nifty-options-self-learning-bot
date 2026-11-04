"""Purged K-fold cross-validation with an embargo (for time-series labels).

Samples are events with a start time ``t0`` (when the features are known)
and an end time ``t1`` (when the label is known, e.g. the triple-barrier exit).
Plain K-fold leaks: a training label whose [t0, t1] overlaps the test period
"knows" test-period prices. So for each test fold:

* test fold:  a contiguous block of samples in time order (never shuffled)
* purge:      drop every training sample whose [t0, t1] overlaps
              [first test t0, last test t1]
* embargo:    also drop training samples that start within ``embargo`` after
              the test period ends (serial correlation right after the test
              window leaks back into it)
* ``forward_only=True`` additionally drops every training sample after the
  test fold, giving walk-forward-style CV (train strictly on the past).

Indices refer to the input order, which must be sorted by ``t0``.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import numpy.typing as npt

IndexArray = npt.NDArray[np.int64]


@dataclass(frozen=True)
class PurgedSplit:
    fold: int
    train: IndexArray
    test: IndexArray
    purged: IndexArray  # removed because the label interval overlaps the test period
    embargoed: IndexArray  # removed because it starts inside the embargo after the test period

    @property
    def test_start(self) -> int:
        return int(self.test[0])


class PurgedKFold:
    def __init__(self, n_splits: int = 5, *, embargo: timedelta = timedelta(0), forward_only: bool = False) -> None:
        if n_splits < 2:
            raise ValueError("n_splits must be at least 2")
        if embargo < timedelta(0):
            raise ValueError("embargo cannot be negative")
        self.n_splits = n_splits
        self.embargo = embargo
        self.forward_only = forward_only

    def split(self, t0: Sequence[datetime], t1: Sequence[datetime]) -> Iterator[PurgedSplit]:
        n = len(t0)
        if n != len(t1):
            raise ValueError("t0 and t1 must have the same length")
        if n < self.n_splits:
            raise ValueError(f"{n} samples cannot form {self.n_splits} folds")
        if any(b < a for a, b in zip(t0, t0[1:], strict=False)):
            raise ValueError("samples must be sorted by t0 (chronological; never shuffle time series)")
        if any(e < s for s, e in zip(t0, t1, strict=True)):
            raise ValueError("every label end t1 must be at or after its start t0")
        start = np.array([s.timestamp() for s in t0])
        end = np.array([e.timestamp() for e in t1])
        idx = np.arange(n, dtype=np.int64)
        emb = self.embargo.total_seconds()
        for fold, test in enumerate(np.array_split(idx, self.n_splits)):
            lo, hi = start[test[0]], end[test].max()
            others = np.setdiff1d(idx, test)
            overlaps = (start[others] <= hi) & (end[others] >= lo)
            embargoed = ~overlaps & (start[others] > hi) & (start[others] <= hi + emb)
            keep = ~overlaps & ~embargoed
            if self.forward_only:
                keep &= others < test[0]
            yield PurgedSplit(fold, others[keep], test, others[overlaps], others[embargoed])


def embargo_from_fraction(t0: Sequence[datetime], fraction: float) -> timedelta:
    """An embargo equal to ``fraction`` of the sample period (de Prado's 1%-style rule)."""
    if not 0 <= fraction < 1:
        raise ValueError("fraction must be in [0, 1)")
    if len(t0) < 2:
        return timedelta(0)
    return (t0[-1] - t0[0]) * fraction
