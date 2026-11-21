"""Pre-registered forward shadow variants A and B (research only), version sv.v1.

Fixed on 2026-11-21, BEFORE the variants were implemented or run on any
recorded session. The fingerprint of ``VARIANTS_V1`` is recorded in
``docs/shadow-variants-preregistration.md`` and checked by
``tests/unit/test_shadow_variants_registered.py``: changing any value fails
that test, so a change can only happen as a NEW version with a new
registration (and the evaluation starts again from zero).

Both variants come from ORB study E1 (``docs/orb-e1-findings.md``), where they
were only estimated on history. Here they run on the bot's REAL recorded option
quotes, next to the current ORB's D2 shadow outcome. Nothing here places,
blocks or sizes an order or changes a strategy, a risk limit or the
configuration.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

VARIANTS_VERSION = "sv.v1"
REGISTERED_ON = date(2026, 11, 21)
#: only trades on sessions on or after this day count for the evaluation (the first session after registration)
EVALUATION_FROM = date(2026, 11, 23)

VARIANTS_V1: dict[str, Any] = {
    "version": VARIANTS_VERSION,
    "common": {
        "lots": 1,
        "quotes": "the bot's recorded 5-second option quotes, archived in research.db by archive-quotes",
        "index_bars": "the bot's complete 1-minute NIFTY candles (trading.db, read-only); if a session has none, "
                      "the research store's Upstox 1-minute candles",
        "trade_rules": "the same TradeRules values the D2 shadow uses for orb_options (bot configuration, "
                       "read-only): strike mode, tick, slippage, entry buffer, entry timeout, quote age, square-off",
        "contract": "ATM of the nearest expiry on or after the day (D2 select_contract, strike step 50)",
        "entry": "the D2 simulator's entry: the decision quote is the latest quote at most max_quote_age old (else "
                 "the first within the entry timeout, flagged); no two-sided quote = NO_TWO_SIDED; LIMIT ask + "
                 "entry buffer ticks, filled at ask + slippage ticks while the ask depth covers the order, within "
                 "the entry timeout, else NO_FILL",
        "candle_delay_seconds": 2.0,  # a 1-minute close is acted on 2 s after its bar ends (candle flush grace)
        "exit_fill": "the first recorded quote at or after the exit trigger time; sell at the bid (last price if "
                     "there is no bid; for a stop the lower of bid and last) minus slippage ticks; wait while the "
                     "bid depth is below the order",
        "square_off": "the first recorded quote at or after the configured square-off start (15:10)",
        "no_more_quotes": "END_OF_DATA at the last recorded quote, flagged",
        "costs": "the bot's ChargeCalculator round trip (brokerage, STT, exchange, GST, SEBI, stamp), as D2; "
                 "spread and slippage are inside the fill prices",
        "not_simulated": ["daily loss limits", "cooldowns", "kill switch", "max open trades", "RL trade manager",
                          "liquidity filter refusals (recorded as flags, as D2)"],
    },
    "A": {
        "name": "A_ORB_SOURCE_EXIT",
        "signals": "every orb_options signal of the D2 shadow (sim d2.1); the same contract and the same entry as "
                   "its D2 row (each signal assumed taken)",
        "opening_range": "high / low of the NIFTY 1-minute bars starting 09:15 .. 09:29 (the bot's 15-minute "
                         "range); every minute must be present, else NO_RANGE",
        "stop": "the first 1-minute NIFTY close, of a bar ending at or after the entry fill and before the "
                "square-off, beyond the OPPOSITE side of the range (CE: close < range low; PE: close > range high)",
        "target": None,
        "trailing": None,
        "premium_stop": None,
        "exit_time": "square-off (15:10)",
        "comparison": "paired per signal: A net minus the current ORB's D2 net (both FILLED)",
    },
    "B": {
        "name": "B_RETEST30_LONG_ATM",
        "signals": "ORB study E1 filter B_RETEST_30 (research/orb_study/setups.py, STUDY_E1 e1.v1) on the day's "
                   "NIFTY 1-minute bars: 30-minute range from 09:15, width <= 0.5 % of the 09:15 open, retest "
                   "entry within 60 minutes of the first break, at most one a day, no entry on or after the 15:09 "
                   "bar; a missing range minute = no signal",
        "option": "long ATM: CE for an up retest, PE for a down retest",
        "decision_time": "the retest bar's end + candle_delay_seconds; ATM from that bar's close",
        "stop": "the first later 1-minute close beyond the opposite side of the 30-minute range",
        "target": None,
        "trailing": None,
        "exit_time": "square-off (15:10); E1 estimated B with 15:29 (H7) and 15:10 (H9 hold) exits; the bot "
                     "cannot hold past 15:10",
    },
    "evaluation": {
        "from": EVALUATION_FROM.isoformat(),
        "counted": "FILLED trades on sessions on or after 'from' (A: only signals whose A and D2 rows are both "
                   "FILLED); earlier sessions are shown, marked before registration, and never counted",
        "primary": {"A": "mean paired difference A - current ORB, rupees per lot after charges",
                    "B": "mean net per trade, rupees per lot after charges"},
        "test": "day-block bootstrap interval at 97.5 % (Bonferroni for the 2 primary tests; 10,000 draws, seed "
                "20261123) lies above zero AND the mean is positive in both halves of the counted sample (split "
                "at the median session date)",
        "first_look": {"A": 525, "B": 625},
        "final": {"A": 1050, "B": 1250},
        "sample_size_source": "docs/orb-e1-findings.md: 80 % power at the E1 ESTIMATED effects (A +Rs 302, B "
                              "+Rs 254-298 per trade); first look = half",
        "below_first_look": "not yet evaluable (progress shown only)",
        "no_retuning": "any change is a new version with its own registration; its evaluation starts from zero",
    },
    "reporting": "every run (daily-research.bat): per session the current ORB (D2), A, A - ORB and B (trades, net), "
                 "the running totals since 'from', coverage (signals without range, quotes or fills) and the "
                 "evaluation status",
}


def fingerprint(rules: dict[str, Any] = VARIANTS_V1) -> str:
    """SHA-256 of the canonical JSON of the variants."""
    return hashlib.sha256(json.dumps(rules, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


#: the fingerprint recorded in docs/shadow-variants-preregistration.md
REGISTERED_FINGERPRINT = "301fdc1c208c6afa69e19a21b9f99b2f518bab993789a79ec8bd9b0c7a520a70"

__all__ = ["EVALUATION_FROM", "REGISTERED_FINGERPRINT", "REGISTERED_ON", "VARIANTS_V1", "VARIANTS_VERSION",
           "fingerprint"]
