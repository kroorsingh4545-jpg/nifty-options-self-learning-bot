"""Feature store: point-in-time feature tables for training, and SIGNAL
snapshots of live features for audit and later training.

``FeatureFrame`` is the training table: one row per event, with

* ``event_id``   unique id of the decision (signal / candidate setup)
* ``as_of``      decision time; every feature was computed from data
                 completed at or before it
* ``strategy_id``, ``option_type`` (CE / PE / "" for undirected events)
* every ``FEATURE_NAMES`` column, in schema order (float64, NaN = missing)

Every frame carries its ``data_source`` (``SYNTHETIC`` or ``HISTORICAL``)
and the feature-schema version + hash. Frames from different sources or
schema versions are never mixed, and a saved frame whose schema hash does not
match this code is refused on load (no silent train/serve skew).

``as_of_join`` attaches, to each event, the latest feature row at or before
the event time (never a later one).

``FeatureStore.record_signal`` writes a live feature snapshot to the
``feature_snapshots`` table (mode-scoped, append-only use).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from core.clock import IST
from core.enums import SnapshotPurpose
from data.storage.records import FeatureSnapshotRecord
from data.storage.repositories import FeatureSnapshotRepository
from ml.feature_engineering import FEATURE_NAMES, FEATURE_SCHEMA_VERSION, feature_schema

DataSource = Literal["SYNTHETIC", "HISTORICAL"]
DATA_SOURCES: tuple[str, ...] = ("SYNTHETIC", "HISTORICAL")
KEY_COLUMNS = ("event_id", "as_of", "strategy_id", "option_type")


class FeatureStoreError(ValueError):
    pass


@dataclass(frozen=True)
class FeatureFrame:
    df: pd.DataFrame
    data_source: DataSource
    description: str = ""
    schema_version: str = FEATURE_SCHEMA_VERSION
    schema_hash: str = feature_schema()["hash"]

    def __post_init__(self) -> None:
        if self.data_source not in DATA_SOURCES:
            raise FeatureStoreError(f"data_source must be one of {DATA_SOURCES}, not {self.data_source!r}")
        if self.schema_hash != feature_schema()["hash"] or self.schema_version != FEATURE_SCHEMA_VERSION:
            raise FeatureStoreError(f"feature schema {self.schema_version}/{self.schema_hash} does not match this code "
                                    f"({FEATURE_SCHEMA_VERSION}/{feature_schema()['hash']}); rebuild the features")
        cols = list(self.df.columns)
        expected = [*KEY_COLUMNS, *FEATURE_NAMES]
        if cols[:len(expected)] != expected:
            raise FeatureStoreError(f"columns must start with {expected}; got {cols[:len(expected)]}")
        if self.df["event_id"].duplicated().any():
            raise FeatureStoreError("duplicate event_id")
        if len(self.df):
            ts = self.df["as_of"]
            if not isinstance(ts.dtype, pd.DatetimeTZDtype):
                raise FeatureStoreError("as_of must be timezone-aware")
            if not ts.is_monotonic_increasing:
                raise FeatureStoreError("rows must be sorted by as_of (chronological; never shuffled)")

    @classmethod
    def from_rows(cls, rows: Sequence[Mapping[str, Any]], data_source: DataSource,
                  description: str = "") -> FeatureFrame:
        """Rows of ``{event_id, as_of, strategy_id, option_type, **features}``."""
        cols = [*KEY_COLUMNS, *FEATURE_NAMES]
        df = pd.DataFrame([{c: r.get(c) for c in cols} for r in rows], columns=cols)
        df = _coerce(df)
        return cls(df.sort_values(["as_of", "event_id"], kind="stable").reset_index(drop=True), data_source,
                   description)

    # ------------------------------------------------------------------ #
    @property
    def is_synthetic(self) -> bool:
        return self.data_source == "SYNTHETIC"

    def __len__(self) -> int:
        return len(self.df)

    def matrix(self) -> np.ndarray:
        return self.df.loc[:, list(FEATURE_NAMES)].to_numpy(dtype=np.float64)

    def select(self, mask: Any) -> FeatureFrame:
        return FeatureFrame(self.df.loc[mask].reset_index(drop=True), self.data_source, self.description)

    def concat(self, other: FeatureFrame) -> FeatureFrame:
        if other.data_source != self.data_source:
            raise FeatureStoreError("refusing to mix SYNTHETIC and HISTORICAL features")
        df = pd.concat([self.df, other.df], ignore_index=True).sort_values(["as_of", "event_id"], kind="stable")
        return FeatureFrame(df.reset_index(drop=True), self.data_source, self.description)

    def coverage(self) -> dict[str, float]:
        """Share of non-missing values per feature (data-quality report)."""
        if not len(self.df):
            return dict.fromkeys(FEATURE_NAMES, 0.0)
        return {k: round(float(self.df[k].notna().mean()), 4) for k in FEATURE_NAMES}

    # ------------------------------------------------------------------ #
    def save(self, directory: Path) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        out = self.df.copy()
        out["as_of"] = out["as_of"].map(lambda t: t.isoformat())
        out.to_csv(directory / "features.csv", index=False)
        meta = {"data_source": self.data_source, "description": self.description,
                "schema_version": self.schema_version, "schema_hash": self.schema_hash, "rows": len(self.df)}
        (directory / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        return directory

    @classmethod
    def load(cls, directory: Path) -> FeatureFrame:
        directory = Path(directory)
        meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
        df = pd.read_csv(directory / "features.csv", dtype={"event_id": str, "strategy_id": str, "option_type": str},
                         keep_default_na=False, na_values={k: ["", "nan", "NaN"] for k in FEATURE_NAMES})
        df["as_of"] = pd.to_datetime(df["as_of"], utc=True).dt.tz_convert(IST)
        return cls(_coerce(df), meta["data_source"], meta.get("description", ""), meta["schema_version"],
                   meta["schema_hash"])


def _coerce(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["event_id"] = df["event_id"].astype(str)
    df["strategy_id"] = df["strategy_id"].fillna("").astype(str)
    df["option_type"] = df["option_type"].fillna("").astype(str)
    ts = pd.to_datetime(df["as_of"], utc=True)
    df["as_of"] = ts.dt.tz_convert(IST)
    for k in FEATURE_NAMES:
        df[k] = pd.to_numeric(df[k], errors="coerce").astype(np.float64)
    return df


def as_of_join(events: pd.DataFrame, features: pd.DataFrame, *, on: str = "time", by: str | None = None,
               tolerance: pd.Timedelta | None = None) -> pd.DataFrame:
    """Left-join to each event the latest feature row with ``as_of <= event[on]``
    (backward as-of join; a feature row from after the event is never used)."""
    left = events.sort_values(on, kind="stable")
    right = features.sort_values("as_of", kind="stable")
    out = pd.merge_asof(left, right, left_on=on, right_on="as_of", by=by, direction="backward",
                        allow_exact_matches=True, tolerance=tolerance)
    bad = out["as_of"].notna() & (out["as_of"] > out[on])
    if bad.any():  # cannot happen with direction="backward"; kept as a guard
        raise FeatureStoreError("as-of join produced a feature row from after its event")
    return out


class FeatureStore:
    """Writes live SIGNAL feature snapshots (point-in-time audit trail)."""

    def __init__(self, repo: FeatureSnapshotRepository) -> None:
        self.repo = repo

    def record_signal(self, *, strategy_id: str, instrument_key: str, as_of: datetime, features: Mapping[str, float],
                      extra: Mapping[str, Any] | None = None) -> int:
        payload: dict[str, Any] = {k: (None if v != v else float(v)) for k, v in features.items()}  # NaN -> null
        if extra:
            payload["_meta"] = dict(extra)
        rec = FeatureSnapshotRecord(mode=self.repo.mode, strategy_id=strategy_id, trade_uid=None,
                                    instrument_key=instrument_key, purpose=SnapshotPurpose.SIGNAL, snapshot_time=as_of,
                                    feature_schema_version=FEATURE_SCHEMA_VERSION, features_json=payload)
        return self.repo.insert(rec)


__all__ = ["DATA_SOURCES", "KEY_COLUMNS", "DataSource", "FeatureFrame", "FeatureStore", "FeatureStoreError",
           "as_of_join"]
