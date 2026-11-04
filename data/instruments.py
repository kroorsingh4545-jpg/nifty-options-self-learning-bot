"""Upstox instrument master: the single source of contract specifications.

Lot size, tick size, freeze quantity and expiry for every NIFTY option and
future come from the Upstox instrument list (``NSE.json.gz``) loaded at
startup and refreshed every trading day. **No lot size is ever hard-coded**:
NSE revises lot sizes (e.g. NIFTY went from 75 to 65), and a stale number
would mis-size every trade.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date

from core.models import Instrument
from monitoring.json_logger import get_logger

log = get_logger(__name__)

NIFTY = "NIFTY"


class InstrumentMasterError(LookupError):
    pass


class InstrumentMaster:
    def __init__(self, instruments: Iterable[Instrument], *, loaded_for: date) -> None:
        self.by_key: dict[str, Instrument] = {i.instrument_key: i for i in instruments}
        self.loaded_for = loaded_for

    def __len__(self) -> int:
        return len(self.by_key)

    def get(self, key: str) -> Instrument | None:
        return self.by_key.get(key)

    def lot_size(self, key: str) -> int:
        inst = self.by_key.get(key)
        if inst is None:
            raise InstrumentMasterError(f"instrument {key!r} not in the Upstox instrument list")
        return inst.lot_size

    # ------------------------------------------------------------------ #
    def _nifty(self, kind: str) -> list[Instrument]:
        return [i for i in self.by_key.values() if i.segment == "NSE_FO" and i.underlying_symbol == NIFTY
                and i.instrument_type == kind and i.expiry is not None]

    def nifty_options(self, expiry: date | None = None) -> list[Instrument]:
        opts = self._nifty("CE") + self._nifty("PE")
        return [o for o in opts if expiry is None or o.expiry == expiry]

    def option_expiries(self, today: date) -> list[date]:
        return sorted({o.expiry for o in self.nifty_options() if o.expiry is not None and o.expiry >= today})

    def nearest_option_expiry(self, today: date) -> date:
        exps = self.option_expiries(today)
        if not exps:
            raise InstrumentMasterError("no upcoming NIFTY option expiry in the instrument list")
        return exps[0]

    def nifty_futures(self) -> list[Instrument]:
        return sorted(self._nifty("FUT"), key=lambda f: f.expiry or date.max)

    def current_month_future(self, today: date) -> Instrument:
        """The NIFTY future with the nearest expiry on or after ``today``.

        On expiry day the expiring contract is still the current month; the
        next contract takes over from the following trading day.
        """
        for fut in self.nifty_futures():
            assert fut.expiry is not None
            if fut.expiry >= today:
                return fut
        raise InstrumentMasterError("no unexpired NIFTY futures contract in the instrument list")

    def option_lot_size(self, expiry: date) -> int:
        sizes = {o.lot_size for o in self.nifty_options(expiry)}
        if len(sizes) != 1:
            raise InstrumentMasterError(f"NIFTY options for {expiry} have no single lot size: {sorted(sizes)}")
        return sizes.pop()

    def validate_nifty(self, today: date) -> dict[str, object]:
        """Sanity checks used at startup; raises if the list is unusable."""
        expiry = self.nearest_option_expiry(today)
        lot = self.option_lot_size(expiry)
        fut = self.current_month_future(today)
        if fut.lot_size != lot:
            log.warning("NIFTY futures and options lot sizes differ",
                        extra={"future_lot": fut.lot_size, "option_lot": lot})
        return {"option_expiry": expiry.isoformat(), "option_lot_size": lot,
                "options": len(self.nifty_options(expiry)), "future": fut.trading_symbol,
                "future_key": fut.instrument_key, "future_expiry": fut.expiry.isoformat() if fut.expiry else None}


def build_master(instruments: Sequence[Instrument], today: date) -> InstrumentMaster:
    master = InstrumentMaster(instruments, loaded_for=today)
    if not master.nifty_options():
        raise InstrumentMasterError("instrument list contains no NIFTY options")
    return master
