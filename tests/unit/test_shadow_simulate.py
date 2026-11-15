"""D2: the counterfactual simulator on SYNTHETIC quotes: entries, exits, the
sampling band, replay, and point-in-time behaviour."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, time, timedelta
from typing import Any

import pytest

from core.clock import IST
from research.shadow.rules import TradeRules
from research.shadow.simulate import (
    BAD_LEVELS,
    FILLED,
    NO_FILL,
    NO_QUOTE,
    NO_TWO_SIDED,
    Quote,
    simulate,
)

T0 = datetime(2026, 11, 17, 10, 0, tzinfo=IST)
LOT = 65


def rules(**kw: Any) -> TradeRules:
    base: dict[str, Any] = {"stop_mode": "PREMIUM_PERCENT", "stop_value": 25.0, "target_mode": "R_MULTIPLE",
                            "target_value": 2.0, "trailing_enabled": False, "trailing_mode": "R_MULTIPLE",
                            "trailing_activation": 1.0, "trailing_step": 0.5}
    return TradeRules(**{**base, **kw})


def q(seconds: float, ltp: float, *, spread: float = 0.1, qty: int = 1500, at: datetime = T0) -> Quote:
    t = at + timedelta(seconds=seconds)
    return Quote(t, t, ltp, round(ltp - spread / 2, 2), round(ltp + spread / 2, 2), qty, qty, 250_000, 1_200_000.0)


def path(points: Sequence[tuple[float, float]], *, every: float = 5.0) -> list[Quote]:
    """Linear premium path between (seconds, price) waypoints, one quote every ``every`` s."""
    out = []
    for (s0, p0), (s1, p1) in zip(points, points[1:], strict=False):
        n = int((s1 - s0) // every)
        out += [q(s0 + k * every, round(p0 + (p1 - p0) * k / n, 2)) for k in range(n)]
    out.append(q(points[-1][0], points[-1][1]))
    return out


def cost(qty: int, entry: float, exit_: float) -> float:
    return 40.0 + 0.001 * qty * (entry + exit_)


def run(quotes: list[Quote], r: TradeRules | None = None, **kw: Any) -> Any:
    return simulate(T0, quotes, r or rules(), lot_size=LOT, cost_fn=cost, **kw)


def test_entry_fills_at_the_ask_plus_slippage_and_levels_come_from_the_ask() -> None:
    res = run(path([(0, 100.0), (60, 100.0)]))
    assert res.status == FILLED and res.entry_ref == 100.05 and res.entry_price == 100.1
    assert res.stop == 75.05 and res.target == 150.05 and res.risk_per_unit == 25.0
    assert res.entry_time == T0 and res.quantity == LOT


def test_a_stop_exit_takes_the_recorded_quote_and_reports_the_band() -> None:
    res = run(path([(0, 100.0), (30, 100.0), (60, 60.0)]))
    assert res.status == FILLED and res.exit_reason == "STOP_LOSS"
    # first recorded quote at or below the stop (75.05): 73.33 at 50 s; bid 73.28 - 1 tick
    assert res.exit_price == 73.2 and res.exit_price_best == 75.0  # stop level - 1 tick
    assert res.gross == round((73.2 - 100.1) * LOT, 2)
    assert res.net == round(res.gross - cost(LOT, 100.1, 73.2), 2)
    assert res.r_gross == round((73.2 - 100.1) / 25.0, 3) and res.mae_r is not None and res.mae_r < -1


def test_a_target_is_never_credited_above_the_target_level() -> None:
    res = run(path([(0, 100.0), (20, 100.0), (25, 180.0), (60, 180.0)]))  # gaps far through 150.05
    assert res.exit_reason == "TARGET"
    assert res.exit_price == 150.0 and res.exit_price_best == 179.9  # target - 1 tick .. recorded bid - 1 tick


def test_trailing_stop_exit_is_labelled_and_locks_in_profit() -> None:
    r = rules(trailing_enabled=True, trailing_mode="R_MULTIPLE", trailing_activation=1.0, trailing_step=0.5)
    res = run(path([(0, 100.0), (60, 140.0), (120, 110.0)]), r)
    assert res.exit_reason == "TRAILING_STOP" and res.final_stop is not None and res.final_stop >= 100.05
    assert res.exit_price is not None and res.exit_price > res.entry_price


def test_square_off_at_the_first_quote_from_the_square_off_time() -> None:
    late = datetime(2026, 11, 17, 15, 9, 0, tzinfo=IST)
    quotes = [q(s, 100.0, at=late) for s in range(0, 180, 5)]
    res = simulate(late, quotes, rules(), lot_size=LOT, cost_fn=cost)
    assert res.exit_reason == "SQUARE_OFF" and res.exit_time is not None
    assert res.exit_time.time() == time(15, 10) and res.exit_price == 99.9


def test_no_quote_left_ends_at_the_last_quote() -> None:
    res = run(path([(0, 100.0), (60, 101.0)]))
    assert res.exit_reason == "END_OF_DATA" and "ended_without_exit_trigger" in res.flags


def test_no_decision_quote() -> None:
    assert run([]).status == NO_QUOTE
    assert run([q(-60, 100.0)]).status == NO_QUOTE  # only a stale one
    assert run([q(31, 100.0)]).status == NO_QUOTE  # only after the entry timeout


def test_a_stale_decision_quote_falls_forward_and_is_flagged() -> None:
    res = run([q(-6, 90.0), q(3, 100.0), q(8, 100.0)])
    assert res.status == FILLED and res.entry_ref == 100.05 and "late_decision_quote" in res.flags


def test_one_sided_quote_and_invalid_levels_are_refused_like_the_bot() -> None:
    one_sided = Quote(T0, T0, 100.0, None, 100.05)
    assert run([one_sided]).status == NO_TWO_SIDED
    assert run([q(0, 0.1, spread=0.1)], rules(stop_mode="POINTS", stop_value=5.0)).status == BAD_LEVELS


def test_no_fill_when_the_ask_runs_away_or_depth_is_too_thin() -> None:
    running = [q(0, 100.0, qty=10)] + [q(s, 100.0 + s, qty=1500) for s in range(5, 40, 5)]
    assert run(running).status == NO_FILL  # thin at the decision, then above the limit
    thin = [q(s, 100.0, qty=10) for s in range(0, 40, 5)]
    assert run(thin).status == NO_FILL


def test_the_entry_waits_for_depth_inside_the_timeout() -> None:
    res = run([q(0, 100.0, qty=10), q(5, 100.0, qty=1500), q(10, 100.0)])
    assert res.status == FILLED and res.entry_time == T0 + timedelta(seconds=5)


# Point in time -----------------------------------------------------------------


def test_quotes_after_the_exit_never_change_the_result() -> None:
    base = path([(0, 100.0), (30, 100.0), (60, 60.0)])
    a = run(base)
    b = run(base + [q(s, 500.0) for s in range(65, 300, 5)])
    assert (a.exit_price, a.exit_time, a.net, a.exit_reason) == (b.exit_price, b.exit_time, b.net, b.exit_reason)


def test_quotes_long_before_the_signal_never_change_the_result() -> None:
    base = path([(0, 100.0), (30, 100.0), (60, 60.0)])
    early = [q(-s, 5.0) for s in range(30, 3600, 30)]
    assert run(early + base).net == run(base).net


def test_mfe_and_mae_use_only_the_trade_period() -> None:
    res = run([q(-3, 500.0)] + path([(0, 100.0), (30, 100.0), (60, 60.0)]))
    assert res.mfe_r is not None and res.mfe_r < 1


# Replay ------------------------------------------------------------------------


def test_replay_uses_the_logged_ask_at_the_signal_time() -> None:
    quotes = [q(-4, 98.0)] + path([(1, 100.0), (60, 100.0)])
    pit = run(quotes)
    rep = run(quotes, decision_ask=101.0)
    assert pit.entry_ref == 98.05
    assert rep.entry_ref == 101.0 and rep.entry_price == 101.05 and "replayed_decision_quote" in rep.flags
    assert rep.stop == 75.75


@pytest.mark.parametrize("structural", [True, False])
def test_structural_trailing_is_flagged_not_simulated(structural: bool) -> None:
    r = rules(trailing_enabled=True, trailing_mode="STRUCTURAL" if structural else "R_MULTIPLE")
    res = run(path([(0, 100.0), (60, 100.0)]), r)
    assert ("structural_trailing_not_simulated" in res.flags) is structural
