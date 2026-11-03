"""Broker <-> local reconciliation.

Runs:
* at startup                      -> ``reconcile("startup")``
* after order updates (debounced) -> ``request("order_update")``
* every ``reconcile_interval_seconds`` (<= 5 min) -> ``run_periodic()``
* before and after square-off     -> called by ``SquareOffManager``

Checks:
1. Net open quantity per instrument: local (sum over strategies) == broker.
2. Every local working order exists at the broker; no unknown working
   orders at the broker.
3. UNKNOWN local orders are resolved from the broker's order book.

A mismatch is re-checked once after ``confirm_delay`` (to ride out in-flight
updates); if it persists, the kill switch is triggered. Failure to reach the
broker counts towards the API-failure kill threshold.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from broker.broker_interface import Broker
from broker.errors import BrokerError
from core.clock import now_ist
from core.enums import OrderStatus, RiskSeverity
from data.storage.records import RiskEventRecord
from data.storage.repositories import Repositories
from execution.order_manager import TERMINAL, OrderManager
from execution.position_manager import PositionManager
from monitoring.json_logger import get_logger
from risk.kill_switch import KillReason, KillSwitch

log = get_logger(__name__)


@dataclass(frozen=True)
class Mismatch:
    kind: str  # POSITION_QTY | ORDER_MISSING_AT_BROKER | ORDER_UNKNOWN_LOCALLY | ORDER_UNRESOLVED
    key: str
    local: object
    broker: object


@dataclass
class ReconciliationReport:
    trigger: str
    at: datetime
    ok: bool
    reachable: bool = True
    mismatches: list[Mismatch] = field(default_factory=list)
    resolved_unknown: list[str] = field(default_factory=list)


class Reconciler:
    def __init__(
        self,
        *,
        broker: Broker,
        repos: Repositories,
        orders: OrderManager,
        positions: PositionManager,
        kill_switch: KillSwitch,
        api_result: Callable[[bool, str], None] | None = None,
        debounce_seconds: float = 2.0,
        interval_seconds: float = 300.0,
        confirm_delay: float = 1.0,
        now_fn: Callable[[], datetime] = now_ist,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.broker = broker
        self.repos = repos
        self.orders = orders
        self.positions = positions
        self.kill_switch = kill_switch
        self._api_result = api_result
        self.debounce = debounce_seconds
        self.interval = interval_seconds
        self.confirm_delay = confirm_delay
        self._now = now_fn
        self._sleep = sleep
        self._pending: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self.history: list[ReconciliationReport] = []
        orders.add_listener(lambda rec, u: self.request("order_update"))
        orders.on_needs_reconcile = self.request

    @property
    def last(self) -> ReconciliationReport | None:
        return self.history[-1] if self.history else None

    # ------------------------------------------------------------------ #
    async def _compare(self) -> tuple[list[Mismatch], list[str]]:
        broker_positions = await self.broker.get_positions()
        broker_orders = await self.broker.get_orders()
        mismatches: list[Mismatch] = []
        resolved: list[str] = []

        # 3. resolve UNKNOWN orders first (may change local positions)
        by_coid = {u.client_order_id: u for u in broker_orders if u.client_order_id}
        for rec in self.repos.orders.list_open():
            if rec.status is OrderStatus.UNKNOWN and rec.client_order_id in by_coid:
                self.orders.on_broker_update(by_coid[rec.client_order_id])
                resolved.append(rec.client_order_id)

        # 1. positions
        local = self.positions.net_quantities()
        remote = {p.instrument_key: p.quantity for p in broker_positions if p.quantity != 0}
        for key in sorted(set(local) | set(remote)):
            if local.get(key, 0) != remote.get(key, 0):
                mismatches.append(Mismatch("POSITION_QTY", key, local.get(key, 0), remote.get(key, 0)))

        # 2. orders
        broker_working = {u.client_order_id for u in broker_orders
                          if u.client_order_id and u.status not in TERMINAL}
        for rec in self.repos.orders.list_open():
            coid = rec.client_order_id
            if rec.status is OrderStatus.UNKNOWN:
                if coid not in by_coid:
                    mismatches.append(Mismatch("ORDER_UNRESOLVED", coid, rec.status.value, None))
                continue
            if rec.status is OrderStatus.CREATED:
                continue  # not yet sent
            if coid not in by_coid:
                mismatches.append(Mismatch("ORDER_MISSING_AT_BROKER", coid, rec.status.value, None))
            elif by_coid[coid].status is not rec.status and by_coid[coid].status in TERMINAL:
                self.orders.on_broker_update(by_coid[coid])  # broker already finished it: catch up
        local_known = {o.client_order_id for o in self.repos.orders.list_open()}
        for coid in broker_working - local_known:
            if self.repos.orders.get(coid) is None:
                mismatches.append(Mismatch("ORDER_UNKNOWN_LOCALLY", coid, None, "working"))
        return mismatches, resolved

    async def reconcile(self, trigger: str) -> ReconciliationReport:
        async with self._lock:
            try:
                mismatches, resolved = await self._compare()
                if mismatches and self.confirm_delay > 0:
                    await self._sleep(self.confirm_delay)
                    mismatches, more = await self._compare()
                    resolved += more
            except BrokerError as exc:
                if self._api_result:
                    self._api_result(False, "reconciliation")
                report = ReconciliationReport(trigger, self._now(), ok=False, reachable=False)
                self.history.append(report)
                log.error("reconciliation could not reach broker", extra={"trigger": trigger, "error": str(exc)})
                return report
            if self._api_result:
                self._api_result(True, "reconciliation")
            report = ReconciliationReport(trigger, self._now(), ok=not mismatches, mismatches=mismatches,
                                          resolved_unknown=resolved)
            self.history.append(report)
            if mismatches:
                detail = "; ".join(f"{m.kind} {m.key} local={m.local} broker={m.broker}" for m in mismatches)
                log.critical("reconciliation mismatch", extra={"trigger": trigger, "detail": detail})
                with contextlib.suppress(Exception):
                    self.repos.risk_events.insert(RiskEventRecord(
                        mode=self.repos.mode, event_type="RECONCILIATION_MISMATCH", severity=RiskSeverity.CRITICAL,
                        message=detail[:2000], details_json={"trigger": trigger, "count": len(mismatches)}))
                self.kill_switch.trigger(KillReason.RECONCILIATION_MISMATCH, f"[{trigger}] {detail}"[:500])
            else:
                log.info("reconciliation ok", extra={"trigger": trigger, "resolved_unknown": resolved})
            return report

    # ------------------------------------------------------------------ #
    def request(self, trigger: str = "order_update") -> None:
        """Debounced reconciliation (coalesces bursts of order updates)."""
        if self._pending is not None and not self._pending.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _later() -> None:
            await self._sleep(self.debounce)
            await self.reconcile(trigger)

        self._pending = loop.create_task(_later())

    async def wait_pending(self) -> None:
        if self._pending is not None:
            await asyncio.gather(self._pending, return_exceptions=True)

    async def run_periodic(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._sleep(self.interval)
            if not stop.is_set():
                await self.reconcile("periodic")
