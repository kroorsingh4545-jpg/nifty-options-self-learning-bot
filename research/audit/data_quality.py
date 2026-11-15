"""Data-quality audit of one recorded session (decision-engine D1).

Pure functions over the rows the bot recorded (``option_quotes``,
``market_quotes``, ``signals``) on one day (by RECEIPT time). Nothing here
reads a file, the network or the database: ``research/__main__.py`` passes
the rows in.

What is checked:

* option window completeness: the bot keeps a fixed number of option
  contracts subscribed (ATM +/- the strike band of the nearest expiry, plus
  the next expiry), re-centred as NIFTY moves. Completeness is the share of
  (contract, ``quote_interval_seconds`` slot) pairs in the session that hold
  a sample, against ``expected_option_contracts`` x slots. A contract that
  leaves the window is not a gap; the window running short is.
* market completeness and gaps: the NIFTY index, the future and India VIX
  are always subscribed, so each is checked on its own (share of session
  slots with a sample, and the longest silence).
* missing fields: IV, Greeks, resting total buy / sell quantity, bid / ask
  (options)
* quote sanity: crossed quotes (bid > ask), abnormal spreads, and how often
  the last traded price lies outside the bid / ask of the same message (a
  measure of how far the feed's price and quote are out of step; reported,
  not failed: it is a property of the Upstox snapshot feed)
* stale trades: last-trade time far behind the receipt time (reported)
* duplicates: a sample identical to the one before it (reported)
* clock: median (receipt - exchange time) of the NIFTY index, i.e. network
  latency plus this PC's clock offset
* session coverage: the NIFTY index from the open to the close

A day is CLEAN when every check is within the thresholds in
``research.yaml`` (``data_quality``). Thresholds are review aids, not
trading rules: nothing here can change how the bot trades.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from statistics import median
from typing import Any

Row = Mapping[str, Any]


@dataclass(frozen=True)
class QualityThresholds:
    quote_interval_seconds: float = 5.0
    min_completeness: float = 0.99
    max_gap_seconds: float = 30.0  # always-subscribed instruments (index, future, VIX)
    max_missing_share: float = 0.01  # per field, over the option rows
    abnormal_spread_pct: float = 5.0  # (ask - bid) / mid, in %
    max_abnormal_spread_share: float = 0.01
    stale_trade_seconds: float = 60.0
    max_clock_offset_seconds: float = 2.0
    session_open: time = time(9, 15)
    session_close: time = time(15, 30)
    edge_tolerance_seconds: float = 120.0  # the index must be recorded this close to the open and the close


DEFAULT_THRESHOLDS = QualityThresholds()


def _t(v: Any) -> datetime:
    return datetime.fromisoformat(str(v))


def _share(n: int, d: int) -> float | None:
    return None if d == 0 else n / d


class _Session:
    def __init__(self, day: date, th: QualityThresholds, tz: Any) -> None:
        self.open = datetime.combine(day, th.session_open, tz)
        self.close = datetime.combine(day, th.session_close, tz)
        self.step = th.quote_interval_seconds
        self.slots = max(1, int((self.close - self.open).total_seconds() // self.step))

    def slot(self, at: datetime) -> int | None:
        s = int((at - self.open).total_seconds() // self.step)
        return s if 0 <= s < self.slots else None


def _instrument_stats(rows: Sequence[Row], th: QualityThresholds, sess: _Session) -> dict[str, Any]:
    times = [_t(r["received_at"]) for r in rows]
    inside = [x for x in times if sess.open <= x < sess.close]
    gaps = [(b - a).total_seconds() for a, b in zip(inside, inside[1:], strict=False)]
    if inside:  # the open and the close are edges too
        gaps += [(inside[0] - sess.open).total_seconds(), (sess.close - inside[-1]).total_seconds()]
    two_sided = [r for r in rows if r.get("bid") and r.get("ask")]
    spreads = [100 * (r["ask"] - r["bid"]) / ((r["ask"] + r["bid"]) / 2) for r in two_sided if r["ask"] >= r["bid"]]
    keys = ("exchange_time", "ltp", "bid", "ask", "volume_today", "oi")
    slots = {s for s in (sess.slot(x) for x in times) if s is not None}
    return {
        "rows": len(rows), "first": times[0].isoformat(), "last": times[-1].isoformat(), "slots": len(slots),
        "session_completeness": len(slots) / sess.slots,
        "max_gap_seconds": round(max(gaps), 1) if gaps else None,
        "missing_bid_ask": len(rows) - len(two_sided),
        "crossed": sum(1 for r in two_sided if r["bid"] > r["ask"]),
        "abnormal_spreads": sum(1 for x in spreads if x > th.abnormal_spread_pct),
        "median_spread_pct": round(median(spreads), 4) if spreads else None,
        "ltp_outside_quote": sum(1 for r in two_sided if r["ltp"] < r["bid"] or r["ltp"] > r["ask"]),
        "two_sided": len(two_sided),
        "stale_trades": sum(1 for r in rows if (_t(r["received_at"]) - _t(r["exchange_time"])).total_seconds()
                            > th.stale_trade_seconds),
        "duplicates": sum(1 for a, b in zip(rows, rows[1:], strict=False) if all(a.get(k) == b.get(k) for k in keys)),
        "missing_iv": sum(1 for r in rows if not r.get("iv")),
        "missing_greeks": sum(1 for r in rows if r.get("delta") is None),
        "missing_tbq_tsq": sum(1 for r in rows if r.get("tbq") is None or r.get("tsq") is None),
        "_slots": slots,
    }


def _by_key(rows: Iterable[Row]) -> dict[str, list[Row]]:
    out: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        out[str(r["instrument_key"])].append(r)
    for v in out.values():
        v.sort(key=lambda r: str(r["received_at"]))
    return out


def day_quality(day: date, option_rows: Sequence[Row], market_rows: Sequence[Row], signals: Sequence[Row], *,
                index_key: str, futures_keys: Iterable[str] = (), vix_key: str | None = None,
                expected_option_contracts: int | None = None,
                th: QualityThresholds = DEFAULT_THRESHOLDS) -> dict[str, Any]:
    """The quality report of one recorded session (JSON-ready)."""
    first = next(iter(option_rows or market_rows), None)
    tz = _t(first["received_at"]).tzinfo if first is not None else None
    sess = _Session(day, th, tz)
    opts = {k: _instrument_stats(v, th, sess) for k, v in _by_key(option_rows).items()}
    mkts = {k: _instrument_stats(v, th, sess) for k, v in _by_key(market_rows).items()}
    problems: list[str] = []

    def check(ok: bool, msg: str) -> None:
        if not ok:
            problems.append(msg)

    n_opt = sum(s["rows"] for s in opts.values())
    totals: dict[str, Any] = {"option_rows": n_opt, "option_instruments": len(opts),
                              "market_rows": sum(s["rows"] for s in mkts.values()),
                              "session_slots": sess.slots, "expected_option_contracts": expected_option_contracts}
    check(n_opt > 0, "no option quotes recorded")
    filled = sum(len(s["_slots"]) for s in opts.values())
    window = None
    if expected_option_contracts:
        window = min(1.0, filled / (expected_option_contracts * sess.slots))
        check(window >= th.min_completeness,
              f"option window completeness {window:.3f} < {th.min_completeness} "
              f"({filled:,} of {expected_option_contracts * sess.slots:,} contract-slots)")
    totals["option_window_completeness"] = window
    for field in ("missing_iv", "missing_greeks", "missing_tbq_tsq", "missing_bid_ask"):
        share = _share(sum(s[field] for s in opts.values()), n_opt)
        totals[f"{field}_share"] = share
        if share is not None:
            check(share <= th.max_missing_share, f"options: {field.removeprefix('missing_')} missing in "
                                                 f"{share:.1%} of rows")
    two_sided = sum(s["two_sided"] for s in opts.values())
    totals["ltp_outside_quote_share"] = _share(sum(s["ltp_outside_quote"] for s in opts.values()), two_sided)
    totals["abnormal_spread_share"] = _share(sum(s["abnormal_spreads"] for s in opts.values()), two_sided)
    check((totals["abnormal_spread_share"] or 0.0) <= th.max_abnormal_spread_share,
          f"options: abnormal spreads in {(totals['abnormal_spread_share'] or 0):.1%} of quotes")
    totals["stale_trade_share"] = _share(sum(s["stale_trades"] for s in opts.values()), n_opt)
    totals["duplicate_share"] = _share(sum(s["duplicates"] for s in opts.values()), n_opt)
    for k, s in {**opts, **mkts}.items():
        check(s["crossed"] == 0, f"{k}: {s['crossed']} crossed quotes (bid > ask)")
    for k, s in mkts.items():  # always subscribed: checked one by one
        check(s["session_completeness"] >= th.min_completeness,
              f"{k}: completeness {s['session_completeness']:.3f} < {th.min_completeness}")
        check(s["max_gap_seconds"] is not None and s["max_gap_seconds"] <= th.max_gap_seconds,
              f"{k}: gap of {s['max_gap_seconds']} s")

    idx = _by_key(market_rows).get(index_key, [])
    clock: float | None = None
    if idx:
        clock = median((_t(r["received_at"]) - _t(r["exchange_time"])).total_seconds() for r in idx)
        check((_t(idx[0]["received_at"]) - sess.open).total_seconds() <= th.edge_tolerance_seconds,
              f"NIFTY index first recorded at {idx[0]['received_at']} (open {th.session_open})")
        check((sess.close - _t(idx[-1]["received_at"])).total_seconds() <= th.edge_tolerance_seconds,
              f"NIFTY index last recorded at {idx[-1]['received_at']} (close {th.session_close})")
        check(abs(clock) <= th.max_clock_offset_seconds, f"clock offset {clock:+.2f} s")
    else:
        check(False, "no NIFTY index quotes recorded (market_quotes)")
    check(bool(set(futures_keys) & set(mkts)), "no NIFTY futures quotes recorded (market_quotes)")
    if vix_key is not None:
        check(vix_key in mkts, "no India VIX quotes recorded (market_quotes)")
    totals["index_clock_offset_seconds"] = None if clock is None else round(clock, 3)
    totals["signals"] = len(signals)
    totals["signals_undecided"] = sum(1 for s in signals if not s.get("decided"))
    for s in (*opts.values(), *mkts.values()):
        del s["_slots"]
    return {"day": day.isoformat(), "clean": not problems, "problems": problems, "totals": totals,
            "options": opts, "market": mkts}


def consecutive_clean(reports: Sequence[Mapping[str, Any]]) -> int:
    """How many of the most recent days (in date order) were clean in a row."""
    n = 0
    for rep in sorted(reports, key=lambda r: str(r["day"]), reverse=True):
        if not rep.get("clean"):
            break
        n += 1
    return n


def render_text(rep: Mapping[str, Any]) -> list[str]:
    t = rep["totals"]

    def pct(x: Any) -> str:
        return "n/a" if x is None else f"{100 * x:.1f} %"

    lines = [f"{rep['day']}: {'CLEAN' if rep['clean'] else 'PROBLEMS'} - {t['option_rows']:,} option rows "
             f"({t['option_instruments']} contracts, window completeness {pct(t['option_window_completeness'])}), "
             f"{t['market_rows']:,} market rows, {t['signals']} signals",
             f"  missing: IV {pct(t['missing_iv_share'])}, Greeks {pct(t['missing_greeks_share'])}, "
             f"tbq/tsq {pct(t['missing_tbq_tsq_share'])}, bid/ask {pct(t['missing_bid_ask_share'])}",
             f"  last price outside bid/ask {pct(t['ltp_outside_quote_share'])}, abnormal spreads "
             f"{pct(t['abnormal_spread_share'])}, stale trades {pct(t['stale_trade_share'])}, "
             f"duplicates {pct(t['duplicate_share'])}, index clock offset {t['index_clock_offset_seconds']} s"]
    lines += [f"  - {p}" for p in rep["problems"][:25]]
    if len(rep["problems"]) > 25:
        lines.append(f"  ... and {len(rep['problems']) - 25} more")
    return lines


__all__ = ["DEFAULT_THRESHOLDS", "QualityThresholds", "consecutive_clean", "day_quality", "render_text"]
