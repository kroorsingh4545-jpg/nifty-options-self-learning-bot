"""REFLECT, HYPOTHESIZE and MUTATE on hand-made SYNTHETIC journal rows (no
market data; the rows only exercise the rules)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pytest

from core.clock import IST
from core.enums import EvidenceSource, OptionType, TradingMode
from data.storage.records import TradeJournalRecord
from learning.hypothesis_generator import METRIC_DRAWDOWN, VALIDATION_GATES, HypothesisGenerator
from learning.mutation_engine import (
    NEVER_MUTABLE,
    WHITELIST,
    MutationEngine,
    MutationRefused,
    bump_patch,
    diff,
    dump_strategy,
    never_mutable,
)
from learning.reflection_engine import (
    SYNTHETIC_BANNER,
    FeatureVerdict,
    ReflectionEngine,
    ReportKind,
    render_markdown,
    spearman,
    stats,
    time_bucket,
)
from tests.ml_support import config

DAY = date(2026, 11, 2)  # a Monday
N = 0


def row(net: float, *, sid: str = "orb_options", minute: int = 0, day: date = DAY, hold: int = 10,
        exit_reason: str = "STOP_LOSS", regime: str = "RANGE", r: float | None = None,
        source: EvidenceSource = EvidenceSource.SYNTHETIC, feats: dict[str, Any] | None = None,
        slip_pct: float = 1.0, entry_hour: int = 9, entry_min: int = 35) -> TradeJournalRecord:
    global N
    N += 1
    entry = datetime(day.year, day.month, day.day, entry_hour, entry_min, tzinfo=IST) + timedelta(minutes=minute)
    return TradeJournalRecord(
        trade_uid=f"PAPER-T-{sid}-{N:012d}", mode=TradingMode.PAPER, strategy_id=sid, strategy_version="0.1.0",
        data_source=source, instrument_key="NSE_FO|SYNTH", trading_symbol="NIFTY SYNTH CE", option_type=OptionType.CE,
        strike=25000, expiry=day + timedelta(days=1), quantity=65, entry_time=entry,
        exit_time=entry + timedelta(minutes=hold), entry_price=100, exit_price=100 + net / 65, regime=regime,
        exit_reason=exit_reason, initial_stop_loss=75, final_stop_loss=75, target=150,
        risk_config_json={"stop_loss": {"mode": "PREMIUM_PERCENT", "value": 25}, "take_profit":
                          {"mode": "R_MULTIPLE", "value": 2.0}, "trailing": {"enabled": True, "mode": "R_MULTIPLE",
                                                                             "activation": 1.0, "step": 0.5}},
        gross_pnl=net + 50, costs=50, net_pnl=net, slippage=5,
        r_multiple=r if r is not None else net / 1625, features_entry_json=feats or {},
        execution_json={"slippage_pct_of_risk": slip_pct})


# --------------------------------------------------------------------------- #
# REFLECT
# --------------------------------------------------------------------------- #


def test_stats() -> None:
    s = stats([row(1000), row(-500), row(-500)])
    assert (s.trades, s.wins, s.net_pnl, s.costs, s.expectancy) == (3, 1, 0.0, 150, 0.0)
    assert s.profit_factor == 1.0 and s.max_drawdown == 1000 and s.win_rate_pct == pytest.approx(33.33)
    assert stats([]).trades == 0 and stats([]).expectancy is None


def test_spearman_and_buckets() -> None:
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert spearman([1, 1, 1], [1, 2, 3]) is None
    assert time_bucket(datetime(2026, 11, 2, 9, 47, tzinfo=IST)) == "09:30-10:00"


def test_reflection_report_breakdowns_and_best_worst() -> None:
    rows = [row(800, exit_reason="TARGET", regime="TREND_UP"), row(-400, regime="RANGE"),
            row(-600, regime="RANGE", minute=60), row(300, exit_reason="TRAILING_STOP", regime="TREND_UP")]
    rows.append(row(5000, sid="vwap_ema", exit_reason="TARGET"))  # another strategy: never mixed in
    rep = ReflectionEngine().daily(rows, DAY)
    assert rep.kind is ReportKind.DAILY and set(rep.strategies) == {"orb_options", "vwap_ema"}
    orb = rep.strategies["orb_options"]
    assert orb.stats.trades == 4 and orb.stats.net_pnl == 100
    assert orb.best[0]["net_pnl"] == 800 and orb.worst[0]["net_pnl"] == -600
    assert orb.breakdowns["regime"]["RANGE"]["trades"] == 2
    assert set(orb.breakdowns) >= {"regime", "entry_time", "strike", "stop_target_config", "exit_reason", "version"}
    assert rep.strategies["vwap_ema"].stats.net_pnl == 5000


def test_reports_window_by_exit_day() -> None:
    rows = [row(100), row(100, day=DAY + timedelta(days=2)), row(100, day=DAY + timedelta(days=8))]
    assert ReflectionEngine().daily(rows, DAY).strategies["orb_options"].stats.trades == 1
    weekly = ReflectionEngine().weekly(rows, DAY + timedelta(days=3))
    assert (weekly.start, weekly.end) == (DAY, DAY + timedelta(days=6))
    assert weekly.strategies["orb_options"].stats.trades == 2
    with pytest.raises(ValueError):
        ReflectionEngine().reflect(rows, DAY, DAY - timedelta(days=1))


def test_synthetic_evidence_is_marked_everywhere() -> None:
    rep = ReflectionEngine().daily([row(100)], DAY)
    assert rep.synthetic and rep.to_dict()["banner"] == SYNTHETIC_BANNER
    assert render_markdown(rep).splitlines()[0].startswith(f"> **{SYNTHETIC_BANNER[:30]}")
    real = ReflectionEngine().daily([row(100, source=EvidenceSource.LIVE_FEED)], DAY)
    assert not real.synthetic and SYNTHETIC_BANNER not in render_markdown(real)
    mixed = ReflectionEngine().daily([row(100, source=EvidenceSource.LIVE_FEED), row(1, source=EvidenceSource.UNKNOWN)],
                                     DAY)
    assert mixed.synthetic  # any unverified row taints the report


def test_degradation_and_losing_streak() -> None:
    good = [row(800, minute=i, exit_reason="TARGET") for i in range(6)]
    bad = [row(-600, minute=100 + i) for i in range(6)]
    d = ReflectionEngine().reflect_strategy("orb_options", good + bad).degradation
    assert d.degrading and d.losing_streak == 6
    assert any("expectancy fell" in x for x in d.reasons) and any("losing streak" in x for x in d.reasons)
    assert not ReflectionEngine().reflect_strategy("orb_options", good).degradation.degrading


def test_feature_predictiveness() -> None:
    rows = [row(100 * i - 1000, feats={"rv_15m": float(i), "noise": float((i * 7) % 5), "gap_pct": 1.0},
                minute=i) for i in range(25)]
    found = {f.feature: f.verdict for f in ReflectionEngine().reflect_strategy("orb_options", rows).features}
    assert found["rv_15m"] is FeatureVerdict.PREDICTIVE
    assert found["gap_pct"] is FeatureVerdict.INSUFFICIENT_DATA  # constant: undefined correlation
    few = ReflectionEngine().reflect_strategy("orb_options", rows[:5]).features
    assert all(f.verdict is FeatureVerdict.INSUFFICIENT_DATA for f in few)


def test_execution_issues() -> None:
    rows = [row(-100, slip_pct=25.0), row(100), row(100)]
    ex = ReflectionEngine().reflect_strategy("orb_options", rows).execution
    assert len(ex["heavy_slippage_trades"]) == 1 and ex["issues"]


# --------------------------------------------------------------------------- #
# MUTATE
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def engine() -> MutationEngine:
    return MutationEngine(config())


def test_whitelist_covers_every_build_plan_category() -> None:
    cats = {s.category for s in WHITELIST.values()}
    assert cats == {"entry_threshold", "confirmation", "stop_loss", "target_r_multiple", "trailing_activation",
                    "trailing_step", "size_multiplier", "time_window", "strike_selection", "ml_confidence_threshold"}
    assert not any(never_mutable(p) for p in WHITELIST)  # the two lists never overlap


@pytest.mark.parametrize("path", [
    "allocated_capital", "risk.max_daily_loss", "risk.max_open_trades", "risk.max_trades_per_day",
    "risk.loss_cooldown_minutes", "risk.max_consecutive_losses", "sizing.risk_per_trade", "enabled", "rl.enabled",
    "ml.enabled", "params.signal_instrument", "kill_switch", "time_rules.square_off_start", "global.max_daily_loss",
    "capital.total_capital", "credentials", "max_exposure", "square_off", "version"])
def test_never_mutable(engine: MutationEngine, path: str) -> None:
    assert never_mutable(path)
    with pytest.raises(MutationRefused, match="never mutable"):
        engine.propose("orb_options", {path: 1})


def test_unlisted_paths_are_refused(engine: MutationEngine) -> None:
    with pytest.raises(MutationRefused, match="not a whitelisted"):
        engine.propose("orb_options", {"params.made_up": 1})
    with pytest.raises(MutationRefused, match="does not apply"):  # a VWAP param on ORB
        engine.propose("orb_options", {"params.momentum_bars": 3})
    with pytest.raises(MutationRefused, match="does not apply"):  # ML threshold while ML is off
        engine.propose("orb_options", {"ml.confidence_threshold": 0.65})


@pytest.mark.parametrize(("change", "why"), [
    ({"stop_loss.value": 60}, "outside"),          # above the 50% bound
    ({"stop_loss.value": 25.5}, "grid"),           # off the 1-point grid
    ({"stop_loss.value": 25}, "current value"),
    ({"stop_loss.value": 40}, "at most"),          # +60% in one step
    ({"sizing.size_multiplier": 1.2}, "outside"),  # can never enlarge
    ({"take_profit.value": "x"}, "number"),
    ({"entry_window.end": "15:00"}, "outside"),    # after the 14:45 entry cut-off
    ({"entry_window.start": "09:32"}, "steps"),
    ({"strike_selection.mode": "DEEP"}, "not one of"),
])
def test_bounds_grid_and_step_size(engine: MutationEngine, change: dict[str, Any], why: str) -> None:
    with pytest.raises(MutationRefused, match=why):
        engine.propose("orb_options", change)


def test_a_valid_candidate_changes_only_what_was_asked(engine: MutationEngine) -> None:
    c = engine.propose("orb_options", {"stop_loss.value": 30, "entry_window.end": "10:30"})
    base = dump_strategy(config().strategies.strategies["orb_options"])
    changed = diff(base, c.config_json)
    assert set(changed) == {"stop_loss.value", "entry_window.end", "version"}
    assert c.version == "0.1.1" and c.base_version == "0.1.0"
    assert c.bot_config.strategies.strategies["orb_options"].stop_loss.value == 30
    assert c.bot_config.strategies.strategies["vwap_ema"] == config().strategies.strategies["vwap_ema"]
    assert c.bot_config.risk == config().risk and c.bot_config.settings == config().settings
    assert engine.propose("orb_options", {"stop_loss.value": 30}, taken_versions=["0.1.1"]).version == "0.1.2"


def test_hard_limits_still_apply(engine: MutationEngine) -> None:
    with pytest.raises(MutationRefused, match="ATM"):  # ATM needs offset 0 (config validator)
        engine.propose("orb_options", {"strike_selection.offset_steps": 1})
    with pytest.raises(MutationRefused):  # start after end
        engine.propose("orb_options", {"entry_window.start": "10:25", "entry_window.end": "10:00"})


def test_overlays_only_accept_whitelisted_differences(engine: MutationEngine) -> None:
    c = engine.propose("orb_options", {"stop_loss.value": 30})
    cfg = engine.apply_overlay("orb_options", c.config_json)
    assert cfg.strategies.strategies["orb_options"].version == "0.1.1"
    tampered = dict(c.config_json, allocated_capital=200000)
    with pytest.raises(MutationRefused):
        engine.apply_overlay("orb_options", tampered)
    risky = c.config_json | {"risk": dict(c.config_json["risk"], max_daily_loss=9999)}
    with pytest.raises(MutationRefused):
        engine.apply_overlay("orb_options", risky)
    with pytest.raises(MutationRefused, match="outside"):
        engine.apply_overlay("orb_options", c.config_json | {"stop_loss": {"mode": "PREMIUM_PERCENT", "value": 70}})


def test_bump_patch() -> None:
    assert bump_patch("0.1.0") == "0.1.1"
    assert bump_patch("0.1.0", ["0.1.1", "0.1.2"]) == "0.1.3"


def test_never_mutable_list_names_every_risk_control() -> None:
    for must in ("allocated_capital", "risk", "sizing.risk_per_trade", "kill_switch", "square_off", "credentials",
                 "global", "max_exposure"):
        assert must in NEVER_MUTABLE


# --------------------------------------------------------------------------- #
# HYPOTHESIZE
# --------------------------------------------------------------------------- #


def _reflect(rows: list[TradeJournalRecord]) -> Any:
    return ReflectionEngine().reflect_strategy("orb_options", rows)


def test_quick_stops_produce_a_complete_testable_hypothesis() -> None:
    rows = [row(-500, minute=i, hold=5) for i in range(8)] + [row(900, minute=20 + i, exit_reason="TARGET")
                                                              for i in range(4)]
    res = HypothesisGenerator(config()).generate(_reflect(rows))
    h = next(h for h in res.hypotheses if h.rule == "STOPS_TOO_TIGHT")
    assert h.proposed_change == {"stop_loss.value": 30.0} and h.current_values == {"stop_loss.value": 25.0}
    assert h.strategy_id == "orb_options" and h.base_version == "0.1.0"
    assert h.expected_effect and h.evidence and h.test_metric and h.risk_impact and h.rollback_plan
    assert h.min_sample_size >= config().settings.learning.min_sample_trades
    assert h.validation_plan["gates"] == list(VALIDATION_GATES)
    assert "0.1.0" in h.rollback_plan and h.research_only  # built from SYNTHETIC rows
    MutationEngine(config()).propose("orb_options", h.proposed_change)  # the change is a valid candidate


def test_degradation_hypothesis_only_reduces_size() -> None:
    rows = [row(800, minute=i, exit_reason="TARGET", hold=40) for i in range(6)] + \
           [row(-600, minute=100 + i, hold=40) for i in range(6)]
    res = HypothesisGenerator(config()).generate(_reflect(rows), limit=10)
    h = next(h for h in res.hypotheses if h.rule == "DEGRADING_REDUCE_SIZE")
    assert h.proposed_change["sizing.size_multiplier"] < 1.0 and h.test_metric == METRIC_DRAWDOWN


def test_late_entries_hypothesis_moves_the_window_inside_limits() -> None:
    rows = [row(700, minute=i, exit_reason="TARGET", hold=40) for i in range(5)] + \
           [row(-500, entry_hour=10, entry_min=40, minute=i % 15, hold=40) for i in range(5)]
    res = HypothesisGenerator(config()).generate(_reflect(rows), limit=10)
    h = next(h for h in res.hypotheses if h.rule == "LATE_ENTRIES_LOSE")
    assert h.proposed_change == {"entry_window.end": "10:30"}


def test_logic_findings_become_observations_not_hypotheses() -> None:
    rows = [row(-300, regime="HIGH_VOL", minute=i, hold=40) for i in range(5)] + \
           [row(400, regime="TREND_UP", minute=50 + i, exit_reason="TARGET", hold=40) for i in range(5)]
    res = HypothesisGenerator(config()).generate(_reflect(rows))
    assert any("HIGH_VOL" in o and "logic change" in o for o in res.observations)
    assert all("regime" not in "".join(h.proposed_change) for h in res.hypotheses)


def test_hypotheses_are_capped_and_never_touch_forbidden_paths() -> None:
    rows = [row(-500, minute=i, hold=5) for i in range(20)]
    res = HypothesisGenerator(config()).generate(_reflect(rows))
    assert len(res.hypotheses) <= config().settings.learning.max_hypotheses_per_strategy
    for h in res.hypotheses:
        assert all(p in WHITELIST and not never_mutable(p) for p in h.proposed_change)


def test_no_trades_no_hypotheses() -> None:
    res = HypothesisGenerator(config()).generate(_reflect([]))
    assert res.hypotheses == []
