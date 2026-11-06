# Risk & Execution Audit Report

Date: 2026-11-06 (re-audited 2026-11-06 after the fixes for warnings 1 and 2)
Scope (files / PR): branch `phase-10-finalization` vs `main`.
- re-audit (warning fixes): `monitoring/json_logger.py` (`research_logging`), `monitoring/alerts.py`
  (`AlertLogHandler` masks and skips research records), `runtime/control.py` (published alert text masked),
  `runtime/learning_loop.py` (the worker runs inside `research_logging`); tests in `tests/unit/test_alert_hygiene.py`
- first audit (37 files, +2823/−43): new `runtime/paper.py`,
`runtime/__main__.py`, `runtime/learning_loop.py`, `runtime/notifications.py`, `learning/autonomy_source.py`,
`monitoring/telegram.py`, `core/tasks.py`, Docker files; changed `monitoring/alerts.py`, `risk/kill_switch.py`,
`execution/position_manager.py`, `core/engine.py`, `core/event_bus.py`, `runtime/bot.py`, `runtime/control.py`,
`learning/deployment_controller.py`, `learning/loop.py`, `learning/__main__.py`, `broker/factory.py`,
`data/websocket_manager.py`, `data/storage/repositories.py`, `config/settings.py` + `settings.yaml`.

Mode audited: PAPER. LIVE order routing does not exist.
Data: SYNTHETIC only.
Tests: **1203 passed** (1198 at the first audit + 5 for the fixes); ruff and mypy (strict, 133 files) clean.
Verdict: **PASS WITH WARNINGS**

## Results

| # | Area | Status (OK / WARN / FAIL / N/A) | Evidence (file:line, test) | Notes |
|---|---|---|---|---|
| 1 | Entry logic | OK | The launcher only composes the bot; entries still pass `RiskEntryGateway`. Deployed versions are overlaid with only whitelisted settings allowed to differ (`learning/deployment_controller.py:104`). Test: `test_a_paper_session_trades_the_deployed_version` | A deployed 30% stop was sized to zero lots by the risk gate, which is correct. |
| 2 | Exit logic | OK | Trade listener fires after a trade opens or closes (`execution/position_manager.py:330`, `:404`); listener failures are swallowed (`:184`). Shutdown settles background work, then journals pending trades (`runtime/paper.py:462-468`) | Positions left open across a shutdown are restored at the next start but are not monitored in between (documented). |
| 3 | Stop-loss logic | OK | The trade carries the deployed 22% stop instead of the YAML 25% (launcher test). Restored trades keep the stops stored in the database. | |
| 4 | Take-profit logic | OK | Unchanged | |
| 5 | Trailing-stop logic | OK | Unchanged; restored trades use their stored trailing config. | |
| 6 | Position sizing | OK | Unchanged; the risk budget still caps the size. | |
| 7 | Daily loss limits | OK | Daily-loss kills now raise a `DAILY_LOSS` alert (`risk/kill_switch.py:165`). Test: `test_daily_loss_breach_is_its_own_alert_kind` | |
| 8 | Global loss limits | OK | The overlay leaves risk limits untouched (`eff.risk == cfg.risk` in `test_startup_config_overlays_the_paper_deployment`). | |
| 9 | Margin checks | N/A | Unchanged | |
| 10 | Order lifecycle | OK | Unchanged | |
| 11 | Partial fills | N/A | Unchanged | |
| 12 | Order rejection handling | OK | Unchanged; rejections are WARNING alerts, which Telegram sends. | |
| 13 | Reconciliation | OK | Start-up reconciliation runs inside `bot.prepare()`, which the launcher calls. | |
| 14 | Square-off logic | OK | `bot.schedule()` registers the jobs and the launcher starts the real scheduler (`runtime/paper.py:407`). | The real-time scheduler is not exercised by the new tests (warning 4). |
| 15 | Kill-switch behavior | OK | Feed failure, Upstox auth failure and the rate-limit circuit are wired to the kill switch (`runtime/paper.py:384`, `RiskHooks`). Test: `test_feed_failure_and_upstox_auth_failure_trigger_the_kill_switch`. The event-loop freeze is fixed (`core/tasks.py:25`; `test_task_waiting.py`). | On `main`, 2 tests hang because of the freeze; both pass on this branch. |
| 16 | API rate-limit compliance | OK | `UpstoxRateLimiter.from_config` feeds the REST client, which feeds the feed authoriser; `check --connect` also goes through the limiter. | Telegram is not Upstox and paces itself (~1 message/s, honours HTTP 429). |
| 17 | PAPER/LIVE isolation | OK | The session refuses non-PAPER (`runtime/paper.py:301-302`). The broker comes from the factory (`:377`). No portfolio stream (`:259`). Test: `test_launcher_can_only_build_a_paper_broker`; `TRADING_MODE=PAPER` is pinned in Docker. `DATABASE_PATH` is still checked against the research directories. | |
| 18 | Restart recovery | OK | The start-up autonomy level is the most restrictive of YAML and the last dashboard change (`learning/autonomy_source.py:60`). Deployments are resolved at start-up only. Test: `test_a_paper_session_starts_with_the_persisted_level` | The real Ctrl+C / SIGTERM path is not exercised by tests. |
| 19 | Logging and audit trail | OK | Error-log alerts are masked at the source (`monitoring/alerts.py:115`, `:130`) and every alert is masked again where the dashboard state is built (`runtime/control.py:313`). Telegram text is masked (`monitoring/telegram.py:60`, `:78`). The in-session loop's backtests log at most WARNING, tagged `research_run` / `original_level` (`monitoring/json_logger.py:51-65`, `runtime/learning_loop.py:120`), and never become alerts (`monitoring/alerts.py:112`). Tests: `test_error_log_alerts_are_masked_at_the_source`, `test_dashboard_state_masks_every_alert`, `test_research_logging_downgrades_only_inside_the_context`, `test_a_real_simulated_bot_logs_no_errors_inside_the_context`, `test_the_in_session_loop_raises_no_alerts` (worker thread, own DB, real one-day simulated bot; Telegram fake receives only the real failure logged after the loop) | Warnings 1 and 2 are fixed. |

## Blocking issues

None.

## Warnings

1. **Every real kill sends one extra Telegram message.** `execution/square_off.py:140` logs CRITICAL "kill switch
   flatten finished" even when the flatten succeeds, so the error-log handler forwards it next to the kill alert
   itself. Noise only; the information is correct. (The other half of the first audit's warning 2.)
2. **Positions left open at shutdown are unmonitored until the next start.** This is documented, and the operator is
   told to stop only after the square-off.
3. **Untested start-up paths.** The real-time scheduler, OS signal handling (Ctrl+C / SIGTERM) and the Docker image
   are not exercised by tests. Docker is not installed on the development laptop, so the image was never built.
4. **A heartbeat timestamped in the future counts as fresh** when reading the bot's autonomy level from another
   process (clock skew).
5. **`python -m learning run` checks less than the in-session loop.** It refuses during market hours but not when
   PAPER trades are still open. Trading is unaffected, because deployments only apply at the next start.
6. **The learning loop can slow the heartbeat.** It runs after the close in a worker thread that competes for the
   CPU, so the 30-second Docker health check could flap.
7. **Limits of `research_logging` (new, by design).** It follows the context of the worker thread and the asyncio
   tasks started under it. A plain thread started from inside the loop would not inherit it (none exists today).
   The loop's own ERROR/CRITICAL lines inside the worker are also written at WARNING; a failure of the loop itself
   still propagates and is logged at ERROR on the main thread (`runtime/learning_loop.py` `run_once`), and a deployed
   version that no longer fits is logged CRITICAL again by the launcher at the next start.
8. **Carried warnings:** "HISTORICAL" is still an operator's label on the dataset, not something the code can verify.
   Phase 9 warnings 1, 5 and 6 are unchanged, as are the Phase 5–8 warnings. Phase 9 warnings 2 and 4 and that
   audit's required tests 1 and 2 are now resolved.

Resolved since the first audit:
- **Warning 1 (dashboard alert text not masked) — FIXED.** `AlertLogHandler` masks message and exception text
  before any sink sees it, and `ControlService.build_state` masks every alert's title and message (whatever produced
  it) before the state is published for the dashboard. Tests: `test_error_log_alerts_are_masked_at_the_source`,
  `test_dashboard_state_masks_every_alert`.
- **Warning 2 (in-session loop backtests raised live alerts) — FIXED.** The loop's worker runs inside
  `research_logging`: every record above WARNING is written at WARNING with `research_run` and `original_level`, so
  nothing reaches ERROR/CRITICAL, Telegram or the dashboard. Tests: `test_research_logging_downgrades_only_inside_the_context`
  (other threads and code after the run are unaffected), `test_a_real_simulated_bot_logs_no_errors_inside_the_context`
  (a real simulated bot that logs CRITICAL outside the context logs none inside it),
  `test_the_in_session_loop_raises_no_alerts`.
- **Required test 2 (masking of error-log alerts)** is now covered.

## Required tests missing

1. The end-to-end HISTORICAL path (verified → PAPER_READY → deployed → traded). It needs real data, which doesn't
   exist yet.
2. A short smoke test of `python -m runtime paper` with the real scheduler and signal handling, using fake connections.
3. A Docker build plus `runtime check` run inside the container.

---

### First audit (2026-11-06), for the record

Verdict: PASS WITH WARNINGS, 1198 tests passed, no blocking issues. Its warnings 1 and 2 (the noise half of 2 is
carried as warning 1 above) are fixed. Warnings 3–8 are carried as warnings 2–6 and 8. Its required test 2 is now
covered; required tests 1, 3 and 4 are carried as 1, 2 and 3.
