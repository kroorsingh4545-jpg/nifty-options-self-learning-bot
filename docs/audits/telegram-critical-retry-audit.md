# Risk & Execution Audit Report
Date: 2026-11-28
Scope (files / PR): branch `telegram-critical-retry` vs `main` (ba20030). `monitoring/telegram.py`
(`TelegramAlertSink`: queue, `_deliver`, `_text`, `_backoff`, `aclose`, `describe`), `config/settings.py` +
`config/settings.yaml` (`alerts.telegram.critical_retry_max_seconds`, default 30), README (*Telegram alerts*), new
`tests/unit/test_telegram_critical_retry.py` (16 tests). No change to `risk/`, `execution/`, `broker/`,
`strategies/`, `runtime/` or existing tests. Owner approval: item A, 2026-11-28.
Verdict: **PASS WITH WARNINGS**

Tests: full suite 2,318 passed (1 pre-existing warning); ruff clean; mypy clean on `monitoring/`, `config/`,
`runtime/`, `execution/`. With the old sink, 13 of the 16 new tests fail. The 3 that pass check behaviour that
must stay the same: non-critical alerts still give up, a rejected token still disables, and the cap is bounded.
The suite ran in 16.5 min instead of about 9 because the PC was under other load at the time (the unrelated
menu tests also ran twice as slow). The slowdown is not from this change.

## Why
On 25 Nov 11:34 and 27 Nov 13:01 the office internet was down for about 1 and 11 minutes. Both kill-switch alerts
were given up after 3 attempts (about 10 s) and never reached Telegram (`logs/bot.jsonl`: "telegram send failed;
retrying" x3, then no sendMessage 200).

## The change
* CRITICAL alerts (kill switch, daily-loss breach, exit failure, CRITICAL log lines) are retried until Telegram
  accepts them. Network errors, timeouts, 429 and 5xx all retry, with back-off of 1, 2, 4, 8, 16 s and then at
  most `critical_retry_max_seconds` (30 s) (`telegram.py:217`, `:221-270`). Delivery is in order: alerts queued
  behind wait and follow.
* A CRITICAL alert delivered at least 60 s late starts with "⏳ DELAYED: Telegram was unreachable; this happened
  N min ago at HH:MM:SS IST." (`_text`, `:208`).
* A full queue drops the **oldest non-critical** alert to make room for a CRITICAL one. Before, it dropped the
  oldest entry, which could be a CRITICAL (`:146`). A CRITICAL is dropped only beyond a hard memory cap of
  4 x `queue_size` (800 alerts), and that is counted in `critical_dropped`.
* No retry when Telegram rejects the bot or chat (401/403/404, the channel is disabled as before) or the message
  itself (other 4xx).
* At shutdown, CRITICAL alerts still undelivered after the close timeout are logged in one ERROR line with their
  titles (`aclose`, `:278`). `describe()` (dashboard "alert channels") shows "N critical waiting for Telegram".
* Non-critical alerts are unchanged: they give up after `max_attempts` (3).

## Results
| # | Area | Status | Evidence (file:line, test) | Notes |
|---|---|---|---|---|
| 1-14 | Entry / exit / stops / sizing / limits / orders / reconciliation / square-off | N/A | no trading code changed | the kill's own exits run while Telegram is down: `test_a_paper_session_kill_during_an_outage_is_delivered_after_it` |
| 15 | Kill switch | OK | the kill switch's actions never wait for Telegram (`send` only appends to the queue); the kill alert is now delivered after an outage | `test_a_kill_alert_survives_an_eleven_minute_outage_and_arrives_in_order`, `test_a_paper_session_kill_during_an_outage_is_delivered_after_it` |
| 16 | API rate limits | OK | no Upstox calls; Telegram pacing unchanged (`min_interval_seconds` between messages), 429 `retry_after` honoured for criticals too | `test_criticals_retry_through_every_temporary_failure[429]`, `test_rate_limit_429_waits_and_retries` (existing) |
| 17 | PAPER/LIVE isolation | N/A | unchanged; the alert channel still imports nothing from trading (architecture test) | `test_telegram_channel_never_sees_upstox_or_trading` (existing) |
| 18 | Restart recovery | WARN | the queue is in memory: a CRITICAL still undelivered when the bot is **stopped** is lost (logged as ERROR) | `test_shutdown_during_an_outage_logs_the_undelivered_criticals` |
| 19 | Logging / audit trail | OK | each failed CRITICAL attempt logs a WARNING with the alert title and next wait; "delivered after retries" WARNING with attempts and lateness; token only in the URL (masked) | `test_shutdown_during_an_outage_logs_the_undelivered_criticals` (no token in logs) |

Non-blocking guarantee: `send` still only appends to the queue under a lock and returns at once. Retrying happens
in the background task and yields on every wait (`test_retrying_never_blocks_the_caller_or_the_event_loop`:
200 sends while the worker retries, each under 50 ms, and the event loop keeps turning).

## Blocking issues
None.

## Warnings
1. While a CRITICAL alert is retrying, alerts queued behind it wait (deliberate, to keep the order). During a long
   outage, non-critical alerts beyond `queue_size` (200) are dropped as before.
2. Undelivered CRITICAL alerts do not survive a bot restart (in-memory queue). They are listed in an ERROR line
   at shutdown. Stopping the bot only after the internet is back avoids this.
3. A CRITICAL log line raised while Telegram is down (the error-log handler) is also retried until delivered.
   Repeats are still de-duplicated over `error_dedupe_seconds` (300 s).
4. Each failed attempt logs a WARNING. An 11-minute outage at 30 s waits adds about 25 lines per stuck alert.

## Required tests missing
None for this change. Not tested against the real Telegram API (MockTransport only).
