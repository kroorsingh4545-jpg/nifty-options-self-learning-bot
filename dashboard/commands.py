"""Dashboard -> bot commands.

The dashboard's only write path: it queues a command in ``control_commands``
and the bot runs it through the risk / execution services. The operator is
always the signed-in dashboard user. Nothing here talks to the broker.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from core.clock import now_ist
from core.enums import AutonomyLevel, ControlCommandType
from core.mode_controller import LIVE_CONFIRMATION_PHRASE
from data.storage.records import ControlCommandRecord
from data.storage.repositories import Repositories

COMMAND_TTL = timedelta(seconds=60)


@dataclass(frozen=True)
class Submitted:
    ok: bool
    message: str
    command_id: int | None = None


class CommandClient:
    def __init__(self, repos: Repositories, operator: str, now_fn: Callable[[], datetime] = now_ist) -> None:
        if not operator or not operator.strip():
            raise ValueError("commands need a signed-in operator")
        self.repos = repos
        self.operator = operator.strip()
        self._now = now_fn

    def _send(self, command: ControlCommandType, *, note: str = "", strategy_id: str | None = None,
              target: str | None = None, payload: dict[str, str] | None = None, expires: bool = True) -> Submitted:
        now = self._now()
        cid = self.repos.commands.enqueue(ControlCommandRecord(
            mode=self.repos.mode, command=command, operator=self.operator, note=note.strip(),
            strategy_id=strategy_id, target=target, payload_json=payload or {}, requested_at=now,
            expires_at=now + COMMAND_TTL if expires else None))
        return Submitted(True, f"{command.value} queued (#{cid}); the bot processes it within a second", cid)

    # ------------------------------------------------------------------ #
    def kill(self, note: str = "", strategy_id: str | None = None) -> Submitted:
        # An emergency stop never expires: acting on it late is still safe.
        return self._send(ControlCommandType.KILL_SWITCH, note=note or "emergency stop from dashboard",
                          strategy_id=strategy_id, expires=False)

    def reset_kill(self, note: str, strategy_id: str | None = None) -> Submitted:
        if not note.strip():
            return Submitted(False, "Explain the reset in the note (required).")
        return self._send(ControlCommandType.RESET_KILL_SWITCH, note=note, strategy_id=strategy_id)

    def exit_position(self, trade_uid: str, note: str = "") -> Submitted:
        if not trade_uid:
            return Submitted(False, "No trade selected.")
        return self._send(ControlCommandType.EXIT_POSITION, target=trade_uid, note=note)

    def engine(self, action: str) -> Submitted:
        cmd = {"start": ControlCommandType.ENGINE_START, "pause": ControlCommandType.ENGINE_PAUSE,
               "stop": ControlCommandType.ENGINE_STOP}.get(action)
        if cmd is None:
            return Submitted(False, f"unknown engine action {action!r}")
        return self._send(cmd)

    def set_autonomy(self, level: str) -> Submitted:
        try:
            lvl = AutonomyLevel(level)
        except ValueError:
            return Submitted(False, f"unknown autonomy level {level!r}")
        return self._send(ControlCommandType.SET_AUTONOMY, payload={"level": lvl.value})

    def request_live(self, confirmation: str, approved_by: str) -> Submitted:
        if confirmation != LIVE_CONFIRMATION_PHRASE:
            return Submitted(False, f"LIVE refused: type exactly '{LIVE_CONFIRMATION_PHRASE}' to confirm.")
        if not approved_by.strip():
            return Submitted(False, "LIVE refused: name the human approving this version for LIVE.")
        return self._send(ControlCommandType.REQUEST_LIVE,
                          payload={"confirmation": confirmation, "approved_by": approved_by.strip()})

    def request_paper(self) -> Submitted:
        return self._send(ControlCommandType.REQUEST_PAPER)
