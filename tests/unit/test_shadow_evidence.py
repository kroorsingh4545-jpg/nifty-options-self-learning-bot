"""D3: per-signal volatility evidence (research/shadow/evidence.py) on SYNTHETIC
quotes priced by the model, so every number can be checked."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from typing import Any

import pytest

from core.clock import IST
from research.shadow.evidence import VolContext, evidence_row, gap_weights, weekend_gap_weight
from volatility.business_time import business_minute_variance, clock_from_sessions, total_implied_variance
from volatility.options import calendar_years, price_var

DAY = date(2026, 11, 13)  # a Friday
EXPIRY = date(2026, 11, 17)  # the Tuesday after
T0 = datetime(2026, 11, 13, 11, 0, tzinfo=IST)
K, S0, IV, W = 25000.0, 25000.0, 0.14, 0.35
LOT = 65


def trading_day(d: date) -> bool:
    return d.weekday() < 5


def weights(day: date, expiry: date) -> list[float]:
    return gap_weights(day, expiry, trading_day, weekend_gap_weight(1.9, W))


def model_mid(t: datetime, spot: float, iv: float = IV) -> float:
    """Business-time-consistent model price at ``t`` (same day as T0)."""
    close = datetime.combine(EXPIRY, time(15, 30), tzinfo=IST)
    clock0 = clock_from_sessions(calendar_years((close - T0).total_seconds() / 60),
                                 (datetime.combine(DAY, time(15, 30), tzinfo=IST) - T0).total_seconds() / 60,
                                 weights(DAY, EXPIRY))
    used = business_minute_variance(IV, clock0, W) * (t - T0).total_seconds() / 60
    v_left = (total_implied_variance(IV, clock0) - used) * (iv / IV) ** 2
    return price_var(spot, K, v_left, calendar_years((close - t).total_seconds() / 60), 0.065, 0.0, True)


def spot_at(t: datetime) -> float:
    return S0 + 0.5 * (t - T0).total_seconds() / 60  # +0.5 point a minute


def ctx(*, feed_iv: float | None = IV, iv_after: float = IV) -> VolContext:
    def quote(key: str, at: datetime) -> Mapping[str, Any] | None:
        if key != "NSE_FO|1CE" or at < T0 - timedelta(seconds=5):
            return None
        iv = IV if at <= T0 else iv_after
        m = model_mid(at, spot_at(at), iv)
        # the feed reports the model IV in its own unit (``feed_iv`` is the entry value in that unit)
        return {"ltp": m, "bid": round(m - 0.1, 2), "ask": round(m + 0.1, 2),
                "iv": None if feed_iv is None else feed_iv * iv / IV}

    def closes(start: datetime, end: datetime) -> list[float]:
        n = int((end - start).total_seconds() // 60)
        return [spot_at(start + timedelta(minutes=k)) for k in range(n + 1)]

    return VolContext(r=0.065, q=0.0, horizon_minutes=30, square_off=time(15, 10), tick_size=0.05, slippage_ticks=1,
                      evidence_version="d3.test", overnight_share=lambda _d: W, gap_weights=weights,
                      option_quote=quote, index_closes=closes, cost_fn=lambda q, a, b: 50.0)


def outcome(**kw: Any) -> dict[str, Any]:
    base = {"signal_id": "SIG-1", "sim_version": "d2.1", "generated_at": T0.isoformat(), "option_type": "CE",
            "instrument_key": "NSE_FO|1CE", "strike": K, "expiry": EXPIRY.isoformat(), "spot": S0, "lot_size": LOT,
            "status": "FILLED", "entry_time": T0.isoformat(), "exit_time": (T0 + timedelta(minutes=40)).isoformat()}
    return {**base, **kw}


def test_gap_weights_count_weekends_and_holidays() -> None:
    assert weights(DAY, EXPIRY) == [pytest.approx(weekend_gap_weight(1.9, W)), 1.0]  # Mon (after a weekend), Tue
    assert weekend_gap_weight(1.9, 0.35) == pytest.approx((1.9 - 0.65) / 0.35)
    assert weekend_gap_weight(1.9, 0.0) == 1.0
    assert gap_weights(DAY, DAY, trading_day, 3.0) == []  # expiry today: no gap left


def test_entry_iv_greeks_and_hold_economics() -> None:
    e = evidence_row(outcome(), ctx())
    assert e is not None
    assert e["iv_feed_unit"] == "decimal" and e["iv_entry"] == IV
    assert e["iv_solved"] == pytest.approx(IV, abs=2e-3)  # solved from the model mid (calendar BS)
    assert 0.45 < e["delta"] < 0.6 and e["vega"] > 0 and e["theta_day"] < 0
    assert e["position_delta_rupees"] == pytest.approx(LOT * e["delta"], abs=0.01)
    assert e["horizon_minutes"] == 30
    assert e["decay_business"] > e["decay_calendar"] > 0
    assert e["costs_per_unit"] == pytest.approx(0.2 + 0.1 + 50.0 / LOT, abs=0.01)  # spread + 2 ticks + charges
    assert e["expected_move_calendar"] < e["expected_move_business"] < e["expected_move_trading"]
    assert e["breakeven_to_move"] == pytest.approx(e["breakeven_points"] / e["expected_move_business"], abs=1e-3)


def test_ex_post_matched_window_and_attribution() -> None:
    e = evidence_row(outcome(), ctx(iv_after=0.13))
    assert e is not None and e["hold_minutes"] == 40.0
    assert e["implied_var_hold"] > 0 and e["realised_var_hold"] >= 0 and e["realised_to_implied"] is not None
    assert e["iv_exit"] == pytest.approx(0.13, abs=2e-3) and e["iv_change_points"] == pytest.approx(-1.0, abs=0.2)
    assert e["pnl_vega"] < 0 and e["pnl_delta"] > 0 and e["pnl_decay"] < 0
    parts = e["pnl_delta"] + e["pnl_gamma"] + e["pnl_vega"] + e["pnl_decay"]
    assert e["pnl_explained"] == pytest.approx(parts, abs=0.01)
    assert abs(e["pnl_residual"]) < 0.1 * abs(e["pnl_actual_mid"]) + 0.5


def test_a_percent_feed_iv_is_converted_and_a_nonsense_one_ignored() -> None:
    pct = evidence_row(outcome(), ctx(feed_iv=IV * 100))
    bad = evidence_row(outcome(), ctx(feed_iv=1.4))
    assert pct is not None and pct["iv_feed_unit"] == "percent" and pct["iv_entry"] == pytest.approx(IV)
    assert bad is not None and bad["iv_feed_unit"] is None and bad["iv_entry"] == pytest.approx(IV, abs=2e-3)


def test_unfilled_signals_get_ex_ante_evidence_only() -> None:
    e = evidence_row(outcome(status="NO_FILL", entry_time=None, exit_time=None), ctx())
    assert e is not None and e["breakeven_points"] is not None and "hold_minutes" not in e


def test_the_horizon_is_capped_at_the_square_off() -> None:
    late = T0.replace(hour=15, minute=0)
    e = evidence_row(outcome(generated_at=late.isoformat(), status="NO_FILL"), ctx(feed_iv=None))
    assert e is not None and e["horizon_minutes"] == 10.0  # 15:00 -> the 15:10 square-off
    assert e["iv_feed"] is None and e["iv_entry"] == e["iv_solved"]  # no feed IV: the solved one is used


def test_no_contract_spot_or_quote_gives_no_evidence() -> None:
    assert evidence_row(outcome(instrument_key=None), ctx()) is None
    assert evidence_row(outcome(spot=None), ctx()) is None
    assert evidence_row(outcome(instrument_key="NSE_FO|OTHER"), ctx()) is None
    assert evidence_row(outcome(expiry="2026-11-12"), ctx()) is None  # already expired


def test_evidence_rows_are_stored_with_missing_fields_as_null(tmp_path: Any) -> None:
    from research.store import ResearchStore, ResearchStoreError

    store = ResearchStore.open(tmp_path / "research.db")
    full = evidence_row(outcome(), ctx())
    partial = evidence_row(outcome(signal_id="SIG-2", status="NO_FILL", entry_time=None, exit_time=None), ctx())
    assert full is not None and partial is not None
    assert store.upsert_evidence([full, partial]) == 2
    assert store.upsert_evidence([full, partial]) == 2  # recomputed in place
    rows = {r["signal_id"]: r for r in store.evidence_rows("d2.1", "d3.test")}
    assert set(rows) == {"SIG-1", "SIG-2"} and rows["SIG-2"]["hold_minutes"] is None
    assert rows["SIG-1"]["iv_entry"] == IV
    with pytest.raises(ResearchStoreError):
        store.upsert_evidence([{**full, "made_up": 1}])
    store.close()
