"""Pre-registered design of ORB study E1 (research only).

Fixed on 2026-11-21, BEFORE any result of the study was computed. The
fingerprint of ``STUDY_E1`` is recorded in ``docs/orb-e1-preregistration.md``
and checked by ``tests/unit/test_orb_study_registered.py``: changing any value
fails that test, so a design change can only happen as a NEW version with its
own registration. Ideas found after seeing results are POST-HOC: they are
reported as such and only become new hypotheses for forward testing.

The study replicates the "Backtested by Groww" opening-range videos (parts 1
and 2) on our 2022+ NIFTY 1-minute history and compares their exit with ours.
The history OVERLAPS the source's 2020-2026 data, so this is a replication,
not an independent test; independent evidence is forward data only
(``FORWARD_FROM``).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

STUDY_VERSION = "e1.v1"
REGISTERED_ON = date(2026, 11, 21)
#: the only independent evidence: sessions on or after this day (forward shadow, step 6, needs owner approval)
FORWARD_FROM = date(2026, 11, 21)

STUDY_E1: dict[str, Any] = {
    "version": STUDY_VERSION,
    "data": {
        "underlying": "NIFTY 50 index, Upstox 1-minute candles (research store), 2022-01-03 onward",
        "iv_input": "India VIX 1-minute close (decimal = VIX / 100), Upstox, same store",
        "session": {"first_bar": "09:15", "last_bar": "15:29", "min_bars": 370},
        "exclude": "a session whose first bar is not 09:15, whose last bar is not 15:29, or with fewer than "
                   "370 of 375 one-minute bars (special / Muhurat / mock sessions, data gaps); every excluded "
                   "session is listed",
        "missing_minute_inside_range": "the day is skipped for that range length (the range would be wrong)",
        "missing_minute_after_range": "the signal scan uses the bars that exist (a missing minute is no signal)",
        "lot_size": 65,  # the source used 65 for every year
        "years_tested": [2022, 2023, 2024, 2025],
        "partial_year_reported_separately": 2026,
    },
    "rules_source": {
        "range": "high / low of the 1-minute bars from 09:15 for the first N minutes (bars 09:15 .. 09:15+N-1)",
        "range_minutes": [5, 15, 30, 60],
        "scan": "1-minute closes from the first bar after the range; a close above the range high = up break, "
                "below the low = down break (strict inequalities)",
        "entry_price": "the signal bar's close (spot)",
        "stop": "a later 1-minute close beyond the OPPOSITE side of the range; exit at that close",
        "target": None,
        "exit_time": "15:29 (close of the 15:29 bar); an entry on the 15:29 bar is not taken",
        "one_trade_per_day_per_setup": True,
        "BREAKOUT": "the first close beyond the range, in its direction",
        "RETEST": "after the first breakout, a later 1-minute close back INSIDE the range (low <= close <= high), "
                  "then the first later close beyond the SAME side = entry in the original direction "
                  "(literal reading: an intervening close beyond the other side does not cancel it)",
        "REVERSAL": "after the first breakout, the first later close beyond the OPPOSITE side = entry in the "
                    "opposite direction (its stop: a close back beyond the first-break side)",
        "reversal_frequency": "share of days with a first breakout on which a reversal entry occurred",
    },
    "filters_source": {
        "A_BREAKOUT_60": {"setup": "BREAKOUT", "range_minutes": 60, "min_range_pct_of_open": 0.5,
                          "range_rule": "range width > 0.5 % of the day's open (open of the 09:15 bar)",
                          "entry_before": "12:00"},
        "B_RETEST_30": {"setup": "RETEST", "range_minutes": 30, "max_range_pct_of_open": 0.5,
                        "range_rule": "range width <= 0.5 % of the day's open",
                        "max_minutes_after_first_break": 60},
        "C_REVERSAL_5": {"setup": "REVERSAL", "range_minutes": 5, "entry_before": "12:00"},
        "entry_before_rule": "the entry bar closes before 12:00 (bar start <= 11:58)",
    },
    "rules_ours": {
        "source": "the bot's orb_options configuration read by research/__main__.py (read-only) and the "
                  "parity-tested replica research/regime/orb.py; trade rules research/shadow/rules.py",
        "as_of_registration": {"range": "09:15-09:30", "timeframe_minutes": 5, "buffer_points": 5,
                               "entry_window": "09:30-11:00", "max_entries_per_direction": 1,
                               "min_range_points": 10, "max_range_pct": 1.5, "strike": "ATM, step 50",
                               "stop": "25 % of premium", "target": "2 R", "trailing": "R multiple, from 1 R, 0.5 R",
                               "square_off": "15:10"},
        "entry_price": "estimated ATM premium at the signal candle's end (spot = that candle's close)",
        "exit_checks": "every 1-minute bar: stop on the bar's adverse extreme first, then target on the "
                       "favourable extreme, then the trailing stop moves on the favourable extreme (from the next "
                       "bar); a stop / target fills at its level, or at the bar's open price if it gapped through",
        "square_off": "the close of the 15:09 bar (the price at 15:10)",
    },
    "option_estimates": {
        "label": "ESTIMATED, not real option prices",
        "model": "Black-Scholes (European), r = 0.065, q = 0, strike step 50, IV = India VIX (flat: no skew, "
                 "no term structure)",
        "expiry": "the nearest weekly expiry on or after the day (research.sessions.weekly_expiries: Thursday "
                  "until 2025-08-28, Tuesday from 2025-09-02, holiday -> previous trading day)",
        "variance_clock": "D3 business time: total implied variance at entry = VIX^2 x calendar years to the "
                          "15:30 expiry close; each trading minute of the hold uses up the business-time "
                          "minute variance (overnight share w from the trailing 252 sessions, weekend gap weight "
                          "from R2's 1.9 multiple); a VIX change rescales the remaining variance by "
                          "(VIX_t / VIX_entry)^2",
        "assumptions": ["VIX (30-day) stands in for the weekly ATM IV; D3 found VIX about 1.7x too high vs "
                        "realised intraday variance (business clock) - reported, NOT corrected, because option "
                        "prices embed implied, not realised, variance",
                        "no volatility skew: the 0.2-delta strike is priced at the same IV as ATM",
                        "on expiry day the remaining variance is only the rest of the session"],
        "atm": "round(spot / 50) x 50",
        "delta_strike": "the 50-step strike whose model |delta| is nearest 0.20 (ties: further OTM)",
        "structures": {
            "LONG_ATM": "buy the ATM call (bullish) / put (bearish)",
            "DEBIT": "buy ATM, sell the 0.2-delta option of the same type (call spread up, put spread down)",
            "CREDIT": "sell the ATM option of the OPPOSITE type, buy its 0.2-delta protection (bullish = put "
                      "credit spread, bearish = call credit spread)",
        },
        "lots": 1,
    },
    "costs": {
        "model": "backtest/cost_model.py with config costs: brokerage per order, STT (sell), exchange, GST, SEBI, "
                 "stamp (buy); quote = CostModel.quote (half spread 0.25 % of price, min 1 tick); fills buy at "
                 "the ask and sell at the bid, plus paper_broker.slippage_ticks adverse ticks; tick 0.05",
        "orders": {"LONG_ATM": 2, "DEBIT": 4, "CREDIT": 4},
        "spot_points": "H1 / H2 spot points are BEFORE costs (as the source); the pass rule uses after-cost results",
    },
    "hypotheses": {
        "H1": "12 source variants (3 setups x 4 ranges), spot points before costs, vs the source numbers",
        "H2": "the source's 3 filtered setups, spot points before costs, by year",
        "H3": "H1 + H2 as approximate long ATM per lot: 0.5 x points x 65 minus long-ATM round-trip costs "
              "(estimated entry / exit premium as the cost basis)",
        "H4": "our ORB as the bot runs it, estimated long ATM per lot after costs",
        "H5": "our ORB entries with THEIR exit: stop = 1-minute close beyond the opposite side of our 15-minute "
              "range, no target, no trailing, exit 15:10; paired vs H4",
        "H6": "the A_BREAKOUT_60 entries with OUR exit (25 % stop, 2 R target, trailing, 15:10); paired vs the "
              "same entries with their exit (H2 A as estimated long ATM, exit 15:29)",
        "H7": "for each filtered setup: LONG_ATM vs DEBIT vs CREDIT (estimated), source stops / exit 15:29, per "
              "lot after costs; by year, max drawdown, margin estimate, fit in Rs 1,00,000 with our loss limits",
        "H8": "LOW PRIOR, descriptive only: long ATM by trading days to expiry (0, 1, 2, 3, 4+); counts and "
              "by-year signs; no bucket is selected",
        "H9": "fixed spot targets 1R..5R (R = entry to the opposite range side) vs hold to stop / 15:10, on "
              "LONG_ATM and CREDIT, per filtered setup; paired vs hold",
    },
    "pass_rule": {
        "after_costs": "mean per-trade result (rupees per lot, after costs) > 0",
        "years": "right sign in at least 3 of the 4 calendar years 2022-2025 (2026 shown, never counted)",
        "bootstrap": "95 % percentile interval of the mean per-trade result excludes zero; resample whole "
                     "sessions (day blocks) with replacement, 10,000 draws, seed 20261121",
        "comparisons": "a comparison (H5 vs H4, H6 vs H2 A, CREDIT vs DEBIT, CREDIT/DEBIT vs LONG_ATM, a "
                       "target vs hold) uses the SAME entries and tests the paired per-trade difference with "
                       "the same three conditions",
        "applies_to": "H3, H4, H5, H6, H7 and H9 results and their comparisons; H1 / H2 spot points are the "
                      "replication (descriptive); H8 is descriptive only",
    },
    "multiple_testing": {
        "note": "about 50 pass-rule tests are run on one overlapping history; at 5 % each, 2-3 false passes "
                "are expected by chance. The source picked its filters on the same data (in-sample for them).",
        "bonferroni": "each pass is also checked with a Bonferroni interval (alpha = 0.05 / number of pass-rule "
                      "tests actually run); a pass that fails it is labelled WEAK",
        "post_hoc": "anything found after seeing the results is labelled POST-HOC and only becomes a new "
                    "hypothesis for forward shadow testing",
    },
    "reporting": ["every variant tested, including failures", "trades, total, win rate, profit factor, average "
                  "win, average loss, by year", "share of total profit from the top 10 % of days",
                  "max drawdown of the daily cumulative result"],
}


def fingerprint(study: dict[str, Any] = STUDY_E1) -> str:
    """SHA-256 of the canonical JSON of the study design."""
    return hashlib.sha256(json.dumps(study, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


#: the fingerprint recorded in docs/orb-e1-preregistration.md
REGISTERED_FINGERPRINT = "529a524496a9be51d13828d8bad6061b5c3a91c8d14d6f39a7f3fc8952495cf2"

__all__ = ["FORWARD_FROM", "REGISTERED_FINGERPRINT", "REGISTERED_ON", "STUDY_E1", "STUDY_VERSION", "fingerprint"]
