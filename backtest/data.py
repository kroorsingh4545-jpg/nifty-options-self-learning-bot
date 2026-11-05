"""Historical data for the backtester: an instrument list plus 1-minute candles.

A ``HistoricalDataset`` holds:

* ``instruments``: the Upstox instrument list covering the whole period
  (every option and future that was live on any backtest day, with its
  expiry, lot size, tick size and freeze quantity). Lot sizes ALWAYS come from
  here, never from a constant.
* ``candles``: completed 1-minute candles per instrument key (NIFTY index,
  NIFTY futures, NIFTY options), timezone-aware IST, aligned to whole minutes,
  inside the session, in time order, with no duplicates.
* ``source``: ``SYNTHETIC`` or ``HISTORICAL``. Synthetic data is labelled in
  every report.

On-disk format (``load_dataset`` / ``save_dataset``)::

    <dir>/instruments.json   list of Upstox instrument-list records (NSE.json shape)
    <dir>/candles.csv        instrument_key,timestamp,open,high,low,close,volume,oi
    <dir>/meta.json          {"source": "HISTORICAL" | "SYNTHETIC", "description": "..."}

``meta.json`` is optional for backtests (a missing file reads as HISTORICAL),
but ``load_dataset(..., require_source=True)`` refuses a dataset without an
explicit ``source``; the ML layer always loads that way, so a dataset's label
is never guessed before it is used to train a model. A dataset labelled
HISTORICAL whose instruments carry the ``SYNTH`` tag is always refused.

``timestamp`` is the candle START as ISO-8601 with a UTC offset (e.g.
``2026-09-01T09:15:00+05:30``, the shape Upstox's historical-candle API
returns). Naive timestamps are rejected rather than guessed.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal

from broker.upstox_mapping import parse_instrument
from core.clock import IST, ensure_ist
from core.models import Candle, Instrument

DataSource = Literal["SYNTHETIC", "HISTORICAL"]
DATA_SOURCES: tuple[DataSource, ...] = ("HISTORICAL", "SYNTHETIC")
ONE_MINUTE = timedelta(minutes=1)
#: Marker in the instrument key and trading symbol of every generated
#: (``backtest/synthetic_data.py``) instrument. Real Upstox instruments never
#: carry it.
SYNTH_TAG = "SYNTH"


class BacktestDataError(ValueError):
    """The dataset is unusable (inconsistent, misaligned, or incomplete)."""


def synthetic_tagged(instruments: Iterable[Instrument]) -> list[str]:
    """Instrument keys whose key or trading symbol carries the ``SYNTH`` tag."""
    return [i.instrument_key for i in instruments
            if SYNTH_TAG in i.instrument_key.upper() or SYNTH_TAG in i.trading_symbol.upper()]


@dataclass(frozen=True)
class HistoricalDataset:
    instruments: tuple[Instrument, ...]
    candles: Mapping[str, tuple[Candle, ...]]
    source: DataSource = "HISTORICAL"
    description: str = ""
    _by_day: dict[date, dict[str, tuple[Candle, ...]]] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.source not in DATA_SOURCES:
            raise BacktestDataError(f"dataset source must be HISTORICAL or SYNTHETIC, not {self.source!r}")
        if self.source == "HISTORICAL":
            tagged = synthetic_tagged(self.instruments)
            if tagged:
                raise BacktestDataError(
                    f"dataset is labelled HISTORICAL but {len(tagged)} instrument(s) carry the {SYNTH_TAG} tag "
                    f"(e.g. {tagged[0]}); SYNTHETIC data must be labelled SYNTHETIC")
        by_day: dict[date, dict[str, list[Candle]]] = defaultdict(lambda: defaultdict(list))
        for key, series in self.candles.items():
            for c in series:
                by_day[c.start.date()][key].append(c)
        self._by_day.update({d: {k: tuple(v) for k, v in m.items()} for d, m in sorted(by_day.items())})

    @property
    def is_synthetic(self) -> bool:
        return self.source == "SYNTHETIC"

    def days(self) -> list[date]:
        return list(self._by_day)

    def day(self, d: date) -> dict[str, tuple[Candle, ...]]:
        return self._by_day.get(d, {})

    def instrument(self, key: str) -> Instrument | None:
        return next((i for i in self.instruments if i.instrument_key == key), None)

    def slice(self, days: Iterable[date]) -> HistoricalDataset:
        """Only the given days (walk-forward windows never see other days)."""
        keep = set(days)
        candles = {k: tuple(c for c in v if c.start.date() in keep) for k, v in self.candles.items()}
        return HistoricalDataset(self.instruments, {k: v for k, v in candles.items() if v}, self.source,
                                 self.description)

    # ------------------------------------------------------------------ #
    def validate(self, *, session_open: time, session_close: time, required_keys: Sequence[str] = ()) -> None:
        """Raise ``BacktestDataError`` on anything that could corrupt a backtest."""
        if not self.candles:
            raise BacktestDataError("dataset has no candles")
        if not any(i.option_type is not None for i in self.instruments):
            raise BacktestDataError("instrument list contains no options")
        known = {i.instrument_key for i in self.instruments}
        for key in required_keys:
            if key not in self.candles:
                raise BacktestDataError(f"no candles for required instrument {key}")
        for key, series in self.candles.items():
            inst_needed = key not in required_keys
            if inst_needed and key not in known:
                raise BacktestDataError(f"candles for {key} but it is not in the instrument list")
            prev: Candle | None = None
            for c in series:
                where = f"{key} @ {c.start.isoformat()}"
                if c.timeframe_minutes != 1 or c.end - c.start != ONE_MINUTE:
                    raise BacktestDataError(f"{where}: only complete 1-minute candles are supported")
                if c.start.second or c.start.microsecond:
                    raise BacktestDataError(f"{where}: candle start is not on a whole minute")
                local = c.start.astimezone(IST)
                if not session_open <= local.time() < session_close:
                    raise BacktestDataError(f"{where}: candle outside the {session_open}-{session_close} session")
                if not c.complete:
                    raise BacktestDataError(f"{where}: incomplete candle")
                if prev is not None and c.start <= prev.start:
                    raise BacktestDataError(f"{where}: candles are not strictly increasing in time")
                prev = c


# --------------------------------------------------------------------------- #
# Loading / saving
# --------------------------------------------------------------------------- #

CANDLE_COLUMNS = ("instrument_key", "timestamp", "open", "high", "low", "close", "volume", "oi")


def _parse_ts(raw: str) -> datetime:
    ts = datetime.fromisoformat(raw)
    if ts.tzinfo is None:
        raise BacktestDataError(f"timestamp {raw!r} has no UTC offset; use e.g. 2026-09-01T09:15:00+05:30")
    return ensure_ist(ts)


def candle_from_row(row: Mapping[str, Any]) -> Candle:
    start = _parse_ts(str(row["timestamp"]))
    oi_raw = row.get("oi")
    oi = float(oi_raw) if oi_raw not in (None, "") else None
    return Candle(instrument_key=str(row["instrument_key"]), timeframe_minutes=1, start=start,
                  end=start + ONE_MINUTE, open=float(row["open"]), high=float(row["high"]), low=float(row["low"]),
                  close=float(row["close"]), volume=int(float(row.get("volume") or 0)), oi=oi)


def load_dataset(directory: Path, *, require_source: bool = False) -> HistoricalDataset:
    """Load a dataset directory.

    ``require_source=True`` (always used by the ML layer) refuses a directory
    with no ``meta.json`` or a ``meta.json`` without an explicit ``source``.
    """
    directory = Path(directory)
    if require_source:
        meta_path = directory / "meta.json"
        if not meta_path.is_file():
            raise BacktestDataError(f"{directory} has no meta.json; the data source (HISTORICAL or SYNTHETIC) "
                                    "must be stated explicitly, it is never assumed")
    try:
        records = json.loads((directory / "instruments.json").read_text(encoding="utf-8"))
        meta = json.loads((directory / "meta.json").read_text(encoding="utf-8")) \
            if (directory / "meta.json").is_file() else {}
        instruments = tuple(parse_instrument(r) for r in records)
        series: dict[str, list[Candle]] = defaultdict(list)
        with (directory / "candles.csv").open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            missing = set(CANDLE_COLUMNS[:6]) - set(reader.fieldnames or ())
            if missing:
                raise BacktestDataError(f"candles.csv is missing columns {sorted(missing)}")
            for row in reader:
                c = candle_from_row(row)
                series[c.instrument_key].append(c)
    except (OSError, KeyError, ValueError) as exc:
        if isinstance(exc, BacktestDataError):
            raise
        raise BacktestDataError(f"cannot load dataset from {directory}: {exc}") from exc
    if not isinstance(meta, dict):
        raise BacktestDataError("meta.json must be a JSON object")
    if require_source and "source" not in meta:
        raise BacktestDataError(f"{directory / 'meta.json'} has no \"source\"; state HISTORICAL or SYNTHETIC "
                                "explicitly")
    source = meta.get("source", "HISTORICAL")
    if source not in DATA_SOURCES:
        raise BacktestDataError(f"meta.json source must be HISTORICAL or SYNTHETIC, not {source!r}")
    candles = {k: tuple(sorted(v, key=lambda c: c.start)) for k, v in series.items()}
    return HistoricalDataset(instruments, candles, source, str(meta.get("description", "")))


def instrument_to_record(inst: Instrument) -> dict[str, Any]:
    """Inverse of ``parse_instrument`` (tick size back to paise)."""
    return {
        "instrument_key": inst.instrument_key, "trading_symbol": inst.trading_symbol, "segment": inst.segment,
        "exchange": "NSE", "name": inst.underlying_symbol or "", "underlying_key": inst.underlying_key,
        "underlying_symbol": inst.underlying_symbol, "instrument_type": inst.instrument_type,
        "expiry": inst.expiry.isoformat() if inst.expiry else None, "strike_price": inst.strike,
        "lot_size": inst.lot_size, "minimum_lot": inst.lot_size, "tick_size": round(inst.tick_size * 100, 4),
        "freeze_quantity": inst.freeze_quantity, "weekly": inst.weekly, "underlying_type": "INDEX",
        "exchange_token": inst.instrument_key.split("|")[-1],
    }


def save_dataset(ds: HistoricalDataset, directory: Path) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "instruments.json").write_text(
        json.dumps([instrument_to_record(i) for i in ds.instruments], indent=1), encoding="utf-8")
    (directory / "meta.json").write_text(json.dumps({"source": ds.source, "description": ds.description}, indent=1),
                                         encoding="utf-8")
    with (directory / "candles.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CANDLE_COLUMNS)
        for key in sorted(ds.candles):
            for c in ds.candles[key]:
                w.writerow([key, c.start.isoformat(), c.open, c.high, c.low, c.close, c.volume,
                            "" if c.oi is None else c.oi])
    return directory
