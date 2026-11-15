"""D4: trailing intraday indicators (research/regime/indicators.py) against
reference loops, with point-in-time checks (SYNTHETIC bars)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from research.regime.indicators import (
    adx,
    atr,
    atr_expansion,
    displacement,
    efficiency_ratio,
    realised_sd,
    true_range,
    wilder,
)


def bars(n: int = 120, seed: int = 2) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    c = 24500 * np.exp(np.cumsum(rng.normal(0, 4e-4, n)))
    o = np.concatenate(([c[0]], c[:-1]))
    h = np.maximum(o, c) + rng.uniform(0, 6, n)
    lo = np.minimum(o, c) - rng.uniform(0, 6, n)
    return h, lo, c


def ref_wilder(x: list[float], n: int) -> list[float]:
    out = [math.nan] * len(x)
    s = sum(x[:n]) / n
    out[n - 1] = s
    for i in range(n, len(x)):
        s = (s * (n - 1) + x[i]) / n
        out[i] = s
    return out


def ref_adx(h: np.ndarray, lo: np.ndarray, c: np.ndarray, n: int) -> list[float]:
    """The textbook Wilder ADX, written longhand."""
    pdm, mdm, tr = [], [], []
    for i in range(1, len(c)):
        up, dn = h[i] - h[i - 1], lo[i - 1] - lo[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
        tr.append(max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])))
    sp, sm, st = ref_wilder(pdm, n), ref_wilder(mdm, n), ref_wilder(tr, n)
    dx = [math.nan if math.isnan(a) else 100 * abs(a / t - b / t) / (a / t + b / t) if (a + b) > 0 else 0.0
          for a, b, t in zip(sp, sm, st, strict=True)]
    valid = [d for d in dx if not math.isnan(d)]
    adx_valid = ref_wilder(valid, n)
    out = [math.nan] * len(c)
    for k, v in enumerate(adx_valid):
        if not math.isnan(v):
            out[1 + (n - 1) + k] = v
    return out


def test_efficiency_ratio_extremes_and_definition() -> None:
    line = np.arange(100.0, 130.0)
    assert efficiency_ratio(line, 10)[-1] == pytest.approx(1.0)
    zigzag = np.array([100.0, 101.0] * 15)
    assert efficiency_ratio(zigzag, 10)[-1] == pytest.approx(0.0, abs=0.11)
    _, _, c = bars()
    n, i = 20, 70
    expect = abs(c[i] - c[i - n]) / sum(abs(c[k] - c[k - 1]) for k in range(i - n + 1, i + 1))
    assert efficiency_ratio(c, n)[i] == pytest.approx(expect)
    assert math.isnan(efficiency_ratio(c, n)[n - 1])


def test_wilder_and_atr_match_reference_loops() -> None:
    h, lo, c = bars()
    tr = true_range(h, lo, c)
    assert tr[0] == pytest.approx(h[0] - lo[0])
    for a, b in zip(wilder(tr, 10), ref_wilder(list(tr), 10), strict=True):
        assert (math.isnan(a) and math.isnan(b)) or a == pytest.approx(b)
    assert atr(h, lo, c, 10)[50] == pytest.approx(ref_wilder(list(tr), 10)[50])


def test_adx_matches_the_textbook_definition() -> None:
    h, lo, c = bars(200)
    mine, ref = adx(h, lo, c, 10), ref_adx(h, lo, c, 10)
    assert np.isnan(mine[:19]).all() and not math.isnan(mine[19])  # first value at 2n - 1
    for a, b in zip(mine, ref, strict=True):
        assert (math.isnan(a) and math.isnan(b)) or a == pytest.approx(b, rel=1e-9)
    assert ((mine[19:] >= 0) & (mine[19:] <= 100)).all()


def test_displacement_expansion_and_sd() -> None:
    h, lo, c = bars()
    d = displacement(h, lo, c, 20, 10)
    assert d[60] == pytest.approx((c[60] - c[40]) / atr(h, lo, c, 10)[60])
    x = atr_expansion(h, lo, c, 10, 20)
    assert x[60] == pytest.approx(atr(h, lo, c, 10)[60] / atr(h, lo, c, 20)[60])
    with pytest.raises(ValueError):
        atr_expansion(h, lo, c, 20, 10)
    r = np.diff(np.log(c))
    assert realised_sd(c, 20)[60] == pytest.approx(np.std(r[40:60], ddof=1))


@pytest.mark.parametrize("fn", ["er", "adx", "disp", "atrx", "sd"])
def test_no_value_depends_on_later_bars(fn: str) -> None:
    h, lo, c = bars(150)

    def series(h_: np.ndarray, lo_: np.ndarray, c_: np.ndarray) -> np.ndarray:
        return {"er": lambda: efficiency_ratio(c_, 20), "adx": lambda: adx(h_, lo_, c_, 10),
                "disp": lambda: displacement(h_, lo_, c_, 20, 10), "atrx": lambda: atr_expansion(h_, lo_, c_, 10, 20),
                "sd": lambda: realised_sd(c_, 20)}[fn]()

    full = series(h, lo, c)
    cut = series(h[:90], lo[:90], c[:90])
    np.testing.assert_allclose(full[:90], cut, equal_nan=True)
