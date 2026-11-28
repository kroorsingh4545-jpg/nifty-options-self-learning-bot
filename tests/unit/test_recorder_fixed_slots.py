"""Fixed 5-second recording slots and crossed futures quotes (owner-approved B, 2026-11-28).

* The recorder saves the first tick of each fixed clock slot (:00, :05, ...; 1 s while held): the same slots the
  data-quality audit counts. The old rule ("5 s since the last sample") drifted to one sample every 5-6 s, so
  even an uninterrupted day (26 Nov: index 98.5 %, VIX 94 %, future 89 %, option window 90 %) could not reach
  the 99 % check.
* Crossed quotes on the NIFTY future are a warning, not a failure; on options and the index they still fail.
* A session stopped after 15:31 passes the close-edge check; one stopped at 15:20 does not.

SYNTHETIC ticks only.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any

import pytest

from core.clock import IST
from core.models import Tick
from research.audit.data_quality import QualityThresholds, day_quality, render_text
from runtime.market_recorder import MarketRecorder
from tests.unit.test_decision_d1_recording import CFG, _db, rich_tick
from tests.unit.test_research_d1 import FUT, INDEX, VIX

DAY = date(2026, 11, 26)
OPT, HELD = "NSE_FO|25750PE", "NSE_FO|25800CE"


def t(h: int, m: int, s: float = 0.0) -> datetime:
    return datetime.combine(DAY, time(h, m), tzinfo=IST) + timedelta(seconds=s)


def recorder(repo: Any, held: set[str] | None = None) -> MarketRecorder:
    return MarketRecorder(repo, CFG.settings.market_recording, is_option=lambda k: k.endswith(("CE", "PE")),
                          held_keys=lambda: held or set(), is_market=lambda k: k in {INDEX, FUT, VIX})


def at_receipt(key: str, px: float, when: datetime) -> Tick:
    return rich_tick(key, px, when)


def jittered_stream(start: datetime, end: datetime, mean_gap: float, seed: int) -> list[datetime]:
    """Tick receipt times like the Upstox snapshot feed: irregular, roughly every ``mean_gap`` seconds."""
    rnd = random.Random(seed)  # noqa: S311 - reproducible synthetic test data, not security
    out, now = [], start
    while now < end:
        out.append(now)
        now += timedelta(seconds=rnd.uniform(0.3 * mean_gap, 1.7 * mean_gap))
    return out


def old_rule_samples(times: list[datetime], every: float = 5.0) -> list[datetime]:
    """The previous sampling rule, kept here only to show why the 99 % check was unfair."""
    out: list[datetime] = []
    for x in times:
        if not out or (x - out[-1]).total_seconds() >= every:
            out.append(x)
    return out


def slots(times: list[datetime], every: float = 5.0) -> set[int]:
    return {int(x.timestamp() // every) for x in times}


# --------------------------------------------------------------------------- #
# The recorder
# --------------------------------------------------------------------------- #
def test_one_row_per_fixed_five_second_slot() -> None:
    _, repos = _db()
    rec = recorder(repos.market_history)
    for s in (0.0, 1.2, 4.9, 5.0, 5.1, 9.99, 10.0, 14.0, 15.2):
        rec.on_tick(at_receipt(INDEX, 24500.0 + s, t(10, 0, s)))
    rec.flush()
    got = [r["received_at"] for r in repos.market_history.market_quotes(INDEX)]
    assert [datetime.fromisoformat(str(x)) for x in got] == [t(10, 0, 0.0), t(10, 0, 5.0), t(10, 0, 10.0),
                                                              t(10, 0, 15.2)]


def test_ticks_a_moment_apart_across_a_slot_boundary_are_both_kept() -> None:
    _, repos = _db()
    rec = recorder(repos.market_history)
    rec.on_tick(at_receipt(VIX, 13.0, t(10, 0, 4.95)))
    rec.on_tick(at_receipt(VIX, 13.0, t(10, 0, 5.05)))  # 0.1 s later but a new slot
    rec.on_tick(at_receipt(VIX, 13.0, t(10, 0, 9.90)))  # same slot as 5.05: skipped
    rec.flush()
    assert len(repos.market_history.market_quotes(VIX)) == 2


def test_slots_are_clock_aligned_like_the_audit() -> None:
    open_ = datetime.combine(DAY, time(9, 15), tzinfo=IST)
    assert open_.timestamp() % 5 == 0  # the audit's slots (from 09:15:00) and the recorder's (epoch) coincide


def test_a_silence_is_not_filled_in() -> None:
    _, repos = _db()
    rec = recorder(repos.market_history)
    for s in [0, 1, 2, 3] + [25, 26]:  # 20 s without a tick
        rec.on_tick(at_receipt(FUT, 24560.0, t(10, 0, s)))
    rec.flush()
    assert len(repos.market_history.market_quotes(FUT)) == 2  # slots :00 and :25 only; never invented


def test_held_contracts_use_one_second_slots_and_others_five() -> None:
    _, repos = _db()
    held: set[str] = {HELD}
    rec = recorder(repos.market_history, held)
    for k in range(0, 100):
        x = t(10, 0, k * 0.1)  # ten ticks a second for 10 s
        rec.on_tick(at_receipt(OPT, 100.0, x))
        rec.on_tick(at_receipt(HELD, 90.0, x))
    rec.flush()
    assert len(repos.market_history.quotes(HELD)) == 10 and len(repos.market_history.quotes(OPT)) == 2
    assert all(r["held"] == 1 for r in repos.market_history.quotes(HELD))


def test_snapshots_are_still_not_recorded() -> None:
    _, repos = _db()
    rec = recorder(repos.market_history)
    rec.on_tick(at_receipt(INDEX, 24500.0, t(10, 0)).model_copy(update={"is_snapshot": True}))
    rec.flush()
    assert repos.market_history.market_quotes(INDEX) == []


@pytest.mark.parametrize(("mean_gap", "seed"), [(1.0, 1), (1.3, 2), (0.6, 3)])
def test_a_feed_ticking_about_every_second_now_fills_every_slot(mean_gap: float, seed: int) -> None:
    times = jittered_stream(t(9, 15), t(15, 30), mean_gap, seed)
    every_slot = slots(times)  # slots in which at least one tick arrived
    _, repos = _db()
    rec = recorder(repos.market_history)
    for x in times:
        rec.on_tick(at_receipt(INDEX, 24500.0, x))
    while rec.buffered:
        rec.flush()
    new = [datetime.fromisoformat(str(r["received_at"])) for r in repos.market_history.market_quotes(INDEX)]
    assert slots(new) == every_slot and len(new) == len(every_slot)  # exactly one row per slot with a tick
    old = old_rule_samples(times)
    assert len(slots(old)) < 0.99 * len(every_slot)  # the old rule could not pass the 99 % check


# --------------------------------------------------------------------------- #
# The audit: fair 99 %, crossed futures a warning, the 15:31 stop
# --------------------------------------------------------------------------- #
def _recorded_day(stop: datetime, *, crossed_future: int = 0, crossed_index: int = 0, crossed_option: int = 0,
                  sampler: Callable[[list[datetime]], list[datetime]] | None = None
                  ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Rows for one session as the recorder writes them, from jittered ~1-s ticks."""
    _, repos = _db()
    rec = recorder(repos.market_history)
    keys = (INDEX, FUT, VIX, "NSE_FO|1CE", "NSE_FO|1PE")
    streams = {k: jittered_stream(t(9, 15), stop, 1.0, seed=i) for i, k in enumerate(keys)}
    if sampler is not None:
        streams = {k: sampler(v) for k, v in streams.items()}
    for k, times in streams.items():
        for x in times:
            rec.on_tick(at_receipt(k, 100.0 if k.endswith(("CE", "PE")) else 24500.0, x))
    while rec.buffered:  # each flush writes at most max_rows_per_flush rows
        rec.flush()
    h = repos.market_history
    opts = [dict(r) for k in ("NSE_FO|1CE", "NSE_FO|1PE") for r in h.quotes(k)]
    mkts = [dict(r) for k in (INDEX, FUT, VIX) for r in h.market_quotes(k)]
    for rows, key, n in ((mkts, FUT, crossed_future), (mkts, INDEX, crossed_index), (opts, "NSE_FO|1CE",
                                                                                      crossed_option)):
        for r in [r for r in rows if r["instrument_key"] == key][100:100 + n]:
            r.update(bid=24501.0, ask=24500.0)
    return opts, mkts


def _report(opts: list[dict[str, Any]], mkts: list[dict[str, Any]]) -> dict[str, Any]:
    return day_quality(DAY, opts, mkts, [], index_key=INDEX, futures_keys={FUT}, vix_key=VIX,
                       expected_option_contracts=2, th=QualityThresholds())


def test_an_uninterrupted_session_recorded_with_fixed_slots_is_clean() -> None:
    rep = _report(*_recorded_day(t(15, 31)))
    assert rep["clean"], rep["problems"]
    assert all(m["session_completeness"] >= 0.99 for m in rep["market"].values())
    assert rep["totals"]["option_window_completeness"] >= 0.99


def test_the_same_session_with_the_old_sampling_was_never_clean() -> None:
    def old(times: list[datetime]) -> list[datetime]:  # pre-sample with the old rule; the recorder then keeps them
        return old_rule_samples(times)

    rep = _report(*_recorded_day(t(15, 31), sampler=old))
    assert not rep["clean"] and any("completeness" in p for p in rep["problems"])


def test_crossed_futures_quotes_are_a_warning_not_a_failure() -> None:
    rep = _report(*_recorded_day(t(15, 31), crossed_future=82))
    assert rep["clean"], rep["problems"]
    assert rep["warnings"] == [f"{FUT}: 82 crossed quotes (bid > ask) on the future"]
    text = "\n".join(render_text(rep))
    assert "CLEAN" in text and "~ warning (does not fail the day)" in text and "82 crossed" in text


@pytest.mark.parametrize("where", ["index", "option"])
def test_crossed_index_or_option_quotes_still_fail_the_day(where: str) -> None:
    kw = {"crossed_index": 3} if where == "index" else {"crossed_option": 3}
    rep = _report(*_recorded_day(t(15, 31), **kw))
    assert not rep["clean"] and any("crossed quotes" in p for p in rep["problems"])
    assert rep["warnings"] == []


def test_stopping_after_1531_passes_the_close_edge_and_1520_fails_it() -> None:
    assert _report(*_recorded_day(t(15, 31)))["clean"]
    early = _report(*_recorded_day(t(15, 20)))
    assert not early["clean"] and any("last recorded at" in p for p in early["problems"])


def test_older_reports_without_warnings_still_render() -> None:
    rep = _report(*_recorded_day(t(15, 31)))
    del rep["warnings"]
    assert render_text(rep)[0].startswith("2026-11-26: CLEAN")
