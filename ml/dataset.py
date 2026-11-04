"""Training sets: candidate events -> point-in-time features + labels.

Events (decisions to evaluate) come from either

* ``strategy_events``: the REAL strategy classes (``strategies.registry``)
  replayed on the dataset's completed candles, aggregated to the strategy's
  timeframe and aligned to the session open like ``data.candle_builder``.
  These are the signals the bot would have produced. Every signal is assumed
  approved (it consumes the strategy's per-direction cap).
* ``grid_events``: a dense grid of candidate setups (every N minutes, both
  directions) inside an entry window, for research and the regime model.

For each event, ``build_training_set``

1. selects the option contract with the strategy's strike rules on the
   nearest weekly expiry (same rules as ``runtime.contracts.OptionUniverse``),
2. drops events whose contract fails the strategy's premium bounds, or its
   volume / OI minimums where the data has them (the bot would refuse them),
3. computes features as of the decision time (``compute_features``) with the
   option context from bars completed by then, and recent outcomes from
   earlier events whose labels were KNOWN by then (``t1 <= as_of``),
4. labels the trade with the triple barrier and the next 30 minutes with a
   forward regime label.

Data source (SYNTHETIC / HISTORICAL) is carried through from the dataset.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd

from backtest.data import HistoricalDataset
from broker.charges import ChargeCalculator
from config.settings import BotConfig, StrategyConfig, StrikeSelection
from core.clock import IST
from core.enums import OptionType
from core.models import Candle, Instrument
from ml.feature_engineering import (
    Bars,
    OptionContext,
    PastOutcome,
    RegimeThresholds,
    SessionTimes,
    compute_features,
)
from ml.feature_store import DataSource, FeatureFrame
from ml.labeling import BarrierSpec, forward_regime_label, triple_barrier
from strategies.registry import STRATEGY_CLASSES


@dataclass(frozen=True)
class CandidateEvent:
    event_id: str
    strategy_id: str
    as_of: datetime
    option_type: OptionType | None
    reason: str = ""


def _event_id(strategy_id: str, as_of: datetime, opt: OptionType | None, n: int = 0) -> str:
    raw = f"{strategy_id}|{as_of.isoformat()}|{opt.value if opt else '-'}|{n}"
    return f"EV-{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def session_times(cfg: BotConfig) -> SessionTimes:
    return SessionTimes.from_config(cfg.settings.market_session, cfg.risk.time_rules.square_off_start)


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


def grid_events(days: Iterable[date], *, strategy_id: str, every_minutes: int = 5, start: time = time(9, 45),
                end: time = time(14, 30), directions: Sequence[OptionType | None] = (OptionType.CE, OptionType.PE),
                ) -> list[CandidateEvent]:
    if every_minutes < 1 or not start < end:
        raise ValueError("every_minutes must be >= 1 and start before end")
    out: list[CandidateEvent] = []
    for d in days:
        t = datetime.combine(d, start, tzinfo=IST)
        stop = datetime.combine(d, end, tzinfo=IST)
        while t < stop:
            out.extend(CandidateEvent(_event_id(strategy_id, t, o), strategy_id, t, o, "grid") for o in directions)
            t += timedelta(minutes=every_minutes)
    return out


def aggregate(candles: Sequence[Candle], timeframe: int, session_open: time, session_close: time) -> list[Candle]:
    """N-minute candles from completed 1-minute candles, aligned to the session
    open; a bucket is emitted once its last minute is in (``end`` is capped at
    the close). Same alignment as ``data.candle_builder``."""
    if timeframe == 1:
        return list(candles)
    buckets: dict[datetime, list[Candle]] = {}
    for c in candles:
        local = c.start.astimezone(IST)
        open_dt = datetime.combine(local.date(), session_open, tzinfo=IST)
        k = open_dt + timedelta(minutes=int((local - open_dt).total_seconds() // 60) // timeframe * timeframe)
        buckets.setdefault(k, []).append(c)
    out = []
    for start, cs in sorted(buckets.items()):
        end = min(start + timedelta(minutes=timeframe), datetime.combine(start.date(), session_close, tzinfo=IST))
        if cs[-1].end < end:
            continue  # incomplete bucket (missing minutes at its end)
        out.append(Candle(instrument_key=cs[0].instrument_key, timeframe_minutes=timeframe, start=start, end=end,
                          open=cs[0].open, high=max(c.high for c in cs), low=min(c.low for c in cs), close=cs[-1].close,
                          volume=sum(c.volume for c in cs), oi=cs[-1].oi))
    return out


def current_future(instruments: Iterable[Instrument], day: date) -> Instrument | None:
    """The nearest NIFTY future expiring on/after ``day`` (current month)."""
    futs = [i for i in instruments if i.instrument_type == "FUT" and i.expiry is not None and i.expiry >= day]
    return min(futs, key=lambda i: (i.expiry, i.instrument_key)) if futs else None


def strategy_events(cfg: BotConfig, dataset: HistoricalDataset, strategy_id: str) -> list[CandidateEvent]:
    """Replay the strategy implementation on the dataset (signals only)."""
    scfg = cfg.strategies.strategies[strategy_id]
    cls = STRATEGY_CLASSES[strategy_id]
    session = cfg.settings.market_session
    index_key = cfg.settings.instruments.nifty_index_key
    days = dataset.days()
    if not days:
        return []
    clock = [datetime.combine(days[0], time(9, 0), tzinfo=IST)]
    strategy = cls(strategy_id, scfg.model_copy(update={"enabled": True}), signal_instrument_key=index_key,
                   now_fn=lambda: clock[0])
    uses_futures = str(scfg.params.get("signal_instrument", "NIFTY_INDEX")) == "NIFTY_FUT"
    out: list[CandidateEvent] = []
    for d in days:
        key = index_key
        if uses_futures:
            fut = current_future(dataset.instruments, d)
            if fut is None:
                continue
            key = fut.instrument_key
        strategy.set_signal_instrument(key)
        one = list(dataset.day(d).get(key, ()))
        stream = [c for tf in sorted(strategy.timeframes_used())
                  for c in aggregate(one, tf, session.market_open, session.market_close)]
        stream.sort(key=lambda c: (c.end, c.timeframe_minutes))
        for c in stream:
            clock[0] = c.end
            if not strategy.wants(c):
                continue
            for n, sig in enumerate(strategy.on_candle(c)):
                strategy.on_entry_result(sig, True)
                out.append(CandidateEvent(_event_id(strategy_id, sig.candle_end, sig.option_type, n), strategy_id,
                                          sig.candle_end, sig.option_type, sig.reason))
    return out


# --------------------------------------------------------------------------- #
# Option contracts from the dataset
# --------------------------------------------------------------------------- #


class OptionHistory:
    """Point-in-time option contract lookup over a historical dataset."""

    def __init__(self, dataset: HistoricalDataset) -> None:
        self.dataset = dataset
        self._options = [i for i in dataset.instruments if i.option_type is not None and i.expiry is not None
                         and i.strike is not None]
        self._by: dict[tuple[date, float, OptionType], Instrument] = {}
        for i in self._options:
            assert i.expiry is not None and i.strike is not None and i.option_type is not None
            self._by[(i.expiry, i.strike, i.option_type)] = i
        self._bars: dict[str, Bars] = {}

    def expiry_for(self, day: date) -> date | None:
        """Nearest expiry on/after ``day`` (what the bot trades)."""
        exps = sorted({i.expiry for i in self._options if i.expiry is not None and i.expiry >= day})
        return exps[0] if exps else None

    def select(self, spot: float, opt: OptionType, rules: StrikeSelection, expiry: date) -> Instrument | None:
        strikes = sorted({k[1] for k in self._by if k[0] == expiry and k[2] is opt})
        if not strikes:
            return None
        target = round(spot / rules.strike_step) * rules.strike_step
        atm = min(strikes, key=lambda s: (abs(s - target), s))
        if rules.mode == "ATM" or rules.offset_steps == 0:
            strike = atm
        else:
            sign = -1 if (rules.mode == "ITM") == (opt is OptionType.CE) else 1
            strike = atm + sign * rules.offset_steps * rules.strike_step
        return self._by.get((expiry, strike, opt))

    def bars(self, key: str) -> Bars:
        if key not in self._bars:
            self._bars[key] = Bars.from_candles(self.dataset.candles.get(key, ()))
        return self._bars[key]

    def context(self, inst: Instrument, as_of: datetime) -> OptionContext | None:
        """The contract as seen at ``as_of``: last completed bar's close and OI,
        day volume so far. Spread / feed IV are not in candle data (NaN)."""
        assert inst.option_type is not None and inst.strike is not None and inst.expiry is not None
        today = self.bars(inst.instrument_key).upto(as_of).session(as_of.astimezone(IST).date())
        if not len(today):
            return None
        oi = float(today.oi[-1])
        return OptionContext(inst.option_type, inst.strike, inst.expiry, as_of=as_of, premium=float(today.close[-1]),
                             day_volume=float(today.volume.sum()), oi=None if oi != oi else oi)


def liquidity_reasons(ctx: OptionContext, scfg: StrategyConfig) -> list[str]:
    ss = scfg.strike_selection
    out = []
    if ctx.premium is None or not ss.min_premium <= ctx.premium <= ss.max_premium:
        out.append("premium outside strategy bounds")
    if ctx.day_volume is not None and ctx.day_volume > 0 and ctx.day_volume < ss.liquidity.min_volume:
        out.append("volume below minimum")
    if ctx.oi is not None and ctx.oi < ss.liquidity.min_oi:
        out.append("open interest below minimum")
    return out


# --------------------------------------------------------------------------- #
# Training set
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TrainingSet:
    strategy_id: str
    features: FeatureFrame
    labels: pd.DataFrame  # one row per event_id, same order as features
    spec: BarrierSpec
    regime_horizon_minutes: int
    dropped: dict[str, int] = field(default_factory=dict)

    @property
    def data_source(self) -> DataSource:
        return self.features.data_source

    @property
    def is_synthetic(self) -> bool:
        return self.features.is_synthetic

    def __len__(self) -> int:
        return len(self.features)

    def window(self) -> tuple[datetime, datetime]:
        if not len(self.labels):
            raise ValueError("empty training set")
        return (self.labels["t0"].min().to_pydatetime(), self.labels["t1"].max().to_pydatetime())

    def days(self) -> list[date]:
        return sorted({t.date() for t in self.labels["t0"]})

    def summary(self) -> dict[str, Any]:
        lab = self.labels
        return {"events": len(self), "data_source": self.data_source, "strategy_id": self.strategy_id,
                "days": len(self.days()), "target_first_rate": round(float(lab["target_first"].mean()), 4)
                if len(lab) else None, "loss_rate": round(float(lab["loss"].mean()), 4) if len(lab) else None,
                "hits": dict(Counter(lab["hit"])) if len(lab) else {}, "dropped": dict(self.dropped)}


def futures_bars(dataset: HistoricalDataset) -> Bars:
    """The current-month future's bars, day by day (rolls like the bot)."""
    candles: list[Candle] = []
    for d in dataset.days():
        fut = current_future(dataset.instruments, d)
        if fut is not None:
            candles.extend(dataset.day(d).get(fut.instrument_key, ()))
    return Bars.from_candles(candles)


def build_training_set(cfg: BotConfig, dataset: HistoricalDataset, events: Sequence[CandidateEvent], *,
                       strategy_id: str, max_hold_minutes: int = 60, regime_horizon_minutes: int = 30,
                       thresholds: RegimeThresholds = RegimeThresholds()) -> TrainingSet:  # noqa: B008
    scfg = cfg.strategies.strategies[strategy_id]
    spec = BarrierSpec.from_strategy(scfg, max_hold_minutes)
    times = session_times(cfg)
    index = Bars.from_candles(dataset.candles.get(cfg.settings.instruments.nifty_index_key, ()))
    futs = futures_bars(dataset)
    options = OptionHistory(dataset)
    charges = ChargeCalculator(cfg.settings.costs)
    ticks = cfg.settings.paper_broker.slippage_ticks
    dropped: Counter[str] = Counter()
    outcomes: list[PastOutcome] = []
    rows: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ev in sorted(events, key=lambda e: (e.as_of, e.event_id)):
        if ev.strategy_id != strategy_id:
            raise ValueError(f"event {ev.event_id} belongs to {ev.strategy_id}, not {strategy_id}")
        if ev.event_id in seen:
            continue
        seen.add(ev.event_id)
        if ev.option_type is None:
            raise ValueError("trade-labelled events need a direction (CE / PE)")
        view = index.upto(ev.as_of).session(ev.as_of.astimezone(IST).date())
        if not len(view):
            dropped["no NIFTY bars before the decision"] += 1
            continue
        spot = float(view.close[-1])
        expiry = options.expiry_for(ev.as_of.astimezone(IST).date())
        inst = options.select(spot, ev.option_type, scfg.strike_selection, expiry) if expiry else None
        if inst is None:
            dropped["no contract"] += 1
            continue
        ctx = options.context(inst, ev.as_of)
        if ctx is None:
            dropped["no option bars before the decision"] += 1
            continue
        bad = liquidity_reasons(ctx, scfg)
        if bad:
            dropped[bad[0]] += 1
            continue
        label = triple_barrier(options.bars(inst.instrument_key), ev.as_of, spec, lot_units=inst.lot_size,
                               square_off_start=times.square_off_start, charges=charges,
                               slippage=ticks * inst.tick_size, tick_size=inst.tick_size)
        if label is None:
            dropped["no entry bar / no time to hold"] += 1
            continue
        feats = compute_features(index, ev.as_of, times=times, direction=ev.option_type, futures_bars=futs,
                                 option=ctx, outcomes=outcomes, thresholds=thresholds)
        reg = forward_regime_label(index, ev.as_of, horizon_minutes=regime_horizon_minutes, thresholds=thresholds)
        rows.append({"event_id": ev.event_id, "as_of": ev.as_of, "strategy_id": strategy_id,
                     "option_type": ev.option_type.value, **feats})
        labels.append({"event_id": ev.event_id, **label.as_dict(), "instrument_key": inst.instrument_key,
                       "lot_units": inst.lot_size, "reason": ev.reason,
                       "regime_trend": None if reg is None else reg.trend,
                       "regime_vol": None if reg is None else reg.vol,
                       "regime_t1": None if reg is None else reg.t1})
        outcomes.append(PastOutcome(label.t1, label.net_pnl_per_lot, label.r_multiple))
    frame = FeatureFrame.from_rows(rows, dataset.source, dataset.description)
    lab = pd.DataFrame(labels)
    if len(lab):
        for col in ("t0", "t1", "regime_t1"):
            lab[col] = pd.to_datetime(lab[col], utc=True).dt.tz_convert(IST)
        lab = lab.set_index("event_id").loc[frame.df["event_id"]].reset_index()
    return TrainingSet(strategy_id, frame, lab, spec, regime_horizon_minutes, dict(dropped))


__all__ = ["CandidateEvent", "OptionHistory", "TrainingSet", "aggregate", "build_training_set", "current_future",
           "futures_bars", "grid_events", "liquidity_reasons", "session_times", "strategy_events"]
