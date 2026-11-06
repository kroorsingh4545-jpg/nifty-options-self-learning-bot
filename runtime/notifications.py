"""Trade and end-of-day notifications (Phase 10).

``TradeNotifier`` listens to the position manager and sends an ENTRY alert
when a trade's entry completes and an EXIT alert when it closes.
``end_of_day_alert`` builds the daily summary the PAPER launcher sends after
the close. Both only read trading state and send alerts through the
execution stack's fan-out (memory feed for the dashboard, the JSON log, and
Telegram when configured); a failure here is logged and never reaches
orders, risk or the engine.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

from core.enums import RiskSeverity
from execution.position_manager import ManagedTrade, TradeEvent
from monitoring.alerts import Alert, AlertKind
from monitoring.json_logger import get_logger

if TYPE_CHECKING:
    from runtime.bot import TradingBot

log = get_logger(__name__)


def _rs(x: float) -> str:
    return f"Rs {x:,.2f}"


class TradeNotifier:
    def __init__(self, bot: TradingBot) -> None:
        self.bot = bot
        bot.stack.positions.add_trade_listener(self.on_trade)

    def on_trade(self, event: TradeEvent, t: ManagedTrade) -> None:
        notify = self.bot.stack.notify
        if notify is None:
            return
        try:
            notify.send(self._alert(event, t))
        except Exception:
            log.exception("trade notification failed (trading is unaffected)", extra={"trade": t.trade_uid})

    @staticmethod
    def _alert(event: TradeEvent, t: ManagedTrade) -> Alert:
        sym = t.instrument.trading_symbol
        if event is TradeEvent.OPENED:
            target = f"{t.levels.target:.2f}" if t.levels.target is not None else "none"
            return Alert(RiskSeverity.INFO, f"Entry {sym}",
                         f"BUY {t.filled_qty} {sym} @ {t.avg_entry:.2f} · stop {t.stop:.2f} · target {target}",
                         strategy_id=t.strategy_id, kind=AlertKind.ENTRY, details={"trade": t.trade_uid})
        avg_exit = t.exit_value / t.exited_qty if t.exited_qty else t.avg_entry
        net = (avg_exit - t.avg_entry) * t.exited_qty - t.charges
        risk = t.levels.risk_per_unit
        r = f" ({(avg_exit - t.avg_entry) / risk:+.2f}R)" if risk > 0 else ""
        reason = (t.exit_reason.value if t.exit_reason is not None else "MANUAL")
        return Alert(RiskSeverity.INFO, f"Exit {sym}",
                     f"SELL {t.exited_qty} {sym} @ {avg_exit:.2f} · {reason} · net {_rs(net)}{r} after "
                     f"{_rs(t.charges)} charges", strategy_id=t.strategy_id, kind=AlertKind.EXIT,
                     details={"trade": t.trade_uid})


def end_of_day_alert(bot: TradingBot, day: date | None = None) -> Alert | None:
    """The daily summary, or None on a non-trading day."""
    day = day or bot._now().date()
    if not bot.cfg.settings.market_session.is_trading_day(day):
        return None
    s = bot.stack
    guard: dict[str, Any] = s.guard.snapshot()
    closed = bot.repos.trades.list_closed_on(day)
    lines = []
    total = 0.0
    for sid, g in sorted(guard["strategies"].items()):
        mine = [t for t in closed if t.strategy_id == sid]
        wins = sum(1 for t in mine if (t.net_pnl or 0) > 0)
        net = sum(float(t.net_pnl or 0) for t in mine)
        total += net
        flag = " · LOSS LIMIT HIT" if g.get("breached") else ""
        lines.append(f"{sid}: {len(mine)} trades ({wins} won) · net {_rs(net)} · loss used {_rs(g['loss'])} of "
                     f"{_rs(float(g['limit']))}{flag}")
    still_open = s.positions.open_trade_count()
    ks = s.kill_switch
    if ks.global_event is not None:
        kill = f"active: GLOBAL {ks.global_event.reason.value}"
    elif ks.strategy_events:
        kill = "active: " + ", ".join(f"{k} {v.reason.value}" for k, v in ks.strategy_events.items())
    else:
        kill = "not active"
    lines += [f"Total net P&L: {_rs(total)} · global loss used {_rs(float(guard['global_loss']))} of "
              f"{_rs(float(guard['global_limit']))}",
              f"Open trades after the close: {still_open}", f"Kill switch: {kill}"]
    if bot.learning is not None:
        st = bot.learning.stats()
        lines.append(f"Journal: {st['journaled']} trades journaled this session, {st['failures']} write failures")
    sev = RiskSeverity.WARNING if still_open or ks.active or guard["global_breached"] else RiskSeverity.INFO
    return Alert(sev, f"End of day {day.isoformat()}", "\n".join(lines), kind=AlertKind.EOD_SUMMARY)


def send_end_of_day(bot: TradingBot) -> None:
    try:
        alert = end_of_day_alert(bot)
        if alert is not None and bot.stack.notify is not None:
            bot.stack.notify.send(alert)
    except Exception:
        log.exception("end-of-day summary failed (trading is unaffected)")


__all__ = ["TradeNotifier", "end_of_day_alert", "send_end_of_day"]
