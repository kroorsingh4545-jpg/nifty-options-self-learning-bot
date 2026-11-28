# Risk & Execution Audit Report
Date: 2026-11-28
Scope (files / PR): branch `telegram-control-menu` vs `main` (95b667e). New: `runtime/telegram_menu.py`,
`runtime/feed_stability.py`, `tests/unit/test_telegram_menu.py` (60 tests). Changed: `monitoring/telegram.py`
(adds the `TelegramBotApi` long-polling transport; the alert sink is unchanged), `runtime/control.py` (command
source, Telegram reset re-check, `last_state`, feed stability), `execution/operator_controls.py`
(`trigger_kill(source=...)`, default unchanged), `runtime/paper.py` (wiring, `reconnects` / `late_ticks_dropped` in
the connection status), `config/settings.py` + `config/settings.yaml` (`alerts.telegram.menu`), README
(*Telegram menu*). No change to `risk/`, `broker/`, `strategies/`, risk limits or existing tests.
Verdict: **PASS WITH WARNINGS**

Tests: full suite 2,284 passed (1 pre-existing warning); ruff clean on the repository; mypy clean on `runtime/`,
`monitoring/`, `config/`, `execution/`.

## Results
| # | Area | Status | Evidence (file:line, test) | Notes |
|---|---|---|---|---|
| 1 | Entry logic | OK | Pause / Resume queue `ENGINE_PAUSE` / `ENGINE_START` (`telegram_menu.py:596-610`), run by the existing control plane; Resume never bypasses risk | `test_pause_resume_kill_and_exit_run_through_the_control_queue` |
| 2 | Exit logic | OK | Exit one position: the choice is a server-side reference (`_take`, `:519`); Yes re-checks the trade is still OPEN, then queues `EXIT_POSITION` (`controls.exit_position`, unchanged) | `test_exit_choice_rechecks_that_the_position_is_still_open`, `test_dashboard_and_telegram_exit_at_once_sells_once` (one SELL, flat) |
| 3 | Stop-loss logic | OK | unchanged; the menu never touches stops | `test_telegram_down_does_not_affect_trading` |
| 4 | Take-profit logic | N/A | unchanged | - |
| 5 | Trailing-stop logic | N/A | unchanged | - |
| 6 | Position sizing | N/A | unchanged | - |
| 7 | Daily loss limits | OK | not reachable from Telegram; a same-day daily-loss kill still refuses a reset (`operator_controls.py`, unchanged) | Risk view is read-only |
| 8 | Global loss limits | OK | same as 7 | architecture test forbids `cfg.risk` in the menu |
| 9 | Margin checks | N/A | unchanged | - |
| 10 | Order lifecycle | N/A | no order code added; commands go through the control queue | - |
| 11 | Partial fills | N/A | unchanged | - |
| 12 | Order rejection | N/A | unchanged | - |
| 13 | Reconciliation | OK | a Telegram reset uses `reset_kill_switch` (reconciliation first, unchanged) | `test_reset_accepted_on_a_stable_feed_reactivates_strategies_like_the_dashboard` |
| 14 | Square-off | N/A | unchanged | - |
| 15 | Kill switch | OK | Emergency kill = `KILL_SWITCH` (never expires); reset GLOBAL only, refused unless kill active and feed stable >= 120 s (`reset_check`, `:531`), re-checked on Yes and **again by the control plane when it runs** (`control.py:196-199`); the bot never resets itself (owner choice A): recovery message only (`check_recovery`, `:660`) | `test_reset_is_refused_while_the_feed_is_down_or_not_yet_stable`, `test_feed_dropping_between_yes_and_the_run_refuses_the_reset`, `test_a_telegram_reset_queued_by_hand_is_rechecked_by_the_control_plane`, `test_websocket_disconnect_recovery_message_after_two_stable_minutes` (also: reconnect restarts the 2 min; still active after 5 more min), `test_dashboard_and_telegram_reset_at_once_runs_one_reset` |
| 16 | API rate limits | OK | no Upstox call added; Telegram presses rate-limited 1/s and 20/min (`_allow_press`, `:291`) | `test_button_presses_are_rate_limited` |
| 17 | PAPER/LIVE isolation | OK | only five commands can be queued (`ALLOWED_COMMANDS`, `:68`; `_enqueue` refuses others); button ids must fully match `BUTTON_ID` (`:76`); no LIVE / autonomy / config / risk / credential / restart path in the module | `test_only_five_commands_can_be_queued_from_telegram` (AST), `test_no_live_risk_config_or_credential_path_is_reachable_from_telegram`, `test_forged_button_data_is_never_trusted` (15 forged payloads) |
| 18 | Restart recovery | OK | updates sent while the bot was off are skipped (`drain_backlog`, `:197`); confirmation references are in memory only, so an old Yes does nothing | `test_old_updates_are_skipped_at_start` |
| 19 | Logging / audit trail | OK | every Telegram command is a `control_commands` row with operator and `payload_json.source = "telegram"`; "telegram control requested" WARNING; "control command processed" now logs `source`; kill message `telegram (<operator>)`; reset / re-activation / manual-exit risk events carry the operator; ignored chats logged with chat and user id; token masked and only in the URL | `test_unauthorised_chats_and_users_are_ignored_silently_and_logged`, `test_each_control_button_needs_a_confirmation` (operator, source, note), `test_the_token_is_only_in_the_url_and_never_in_messages` |

Security and isolation:
* Only `TELEGRAM_CHAT_ID` is answered (`TelegramBotApi.authorised`, `telegram.py:312`; checked first in
  `handle`, `telegram_menu.py:271`). Other chats get no reply, not even an answered button.
* Outbound long-polling only (`getUpdates`); no webhook, no server, no socket (`test_telegram_transport_is_outbound_long_polling_only`).
  Network access stays in `monitoring/telegram.py` (existing architecture test still passes).
* The menu runs in its own tasks, catches every error (`safe_handle`, `:259`; poll back-off up to 60 s; a 401/403/404
  stops polling), reads the already-published state snapshot, and never awaits trading work. A poll that returns at
  once with nothing yields and waits 1 s (`:230`), so a misbehaving proxy cannot turn it into a busy loop
  (`test_an_instant_empty_poll_never_becomes_a_busy_loop`; found while writing the tests).
* A failing menu factory or start leaves the PAPER session running (`paper.py:640`;
  `test_the_paper_session_starts_and_stops_the_menu`).

## Blocking issues
None.

## Warnings
1. **Kill alerts are lost in a real network outage.** The log shows that on 25 Nov 11:34 and 27 Nov 13:01 both kill
   alerts were given up after 3 attempts (about 10 s) because the office network was down. The outages lasted
   about 1 and 11 minutes. This branch does not change alert retries (task: "as today"). The recovery message (sent
   once the feed is stable) is the first thing the owner would receive. Suggested follow-up: retry CRITICAL
   alerts until delivered, or include the kill reason and time in the recovery message.
2. Feed stability is sampled once a second from the control plane's connection status; "stable" = connected, not
   stale, no reconnect for 120 s. It has not yet been exercised against a real outage (unit tests only).
3. The menu reads the Status view from the last published snapshot (at most about 1 s old). Controls never rely on
   it: they re-check live objects (kill switch, positions, feed tracker) on Yes and in the control plane.
4. Telegram resets cover the GLOBAL kill only. Strategy-scoped kills still need the dashboard (the menu says so).
5. The operator name defaults to the Telegram account name (`@username`), which the user controls in Telegram.
   Set `alerts.telegram.menu.operator_name` for a fixed audit name.

## Required tests missing
1. No live test against the real Telegram API (all tests use `httpx.MockTransport`). First real use: send *Menu*,
   press Status, then Pause -> No, to confirm buttons render and replies arrive.
