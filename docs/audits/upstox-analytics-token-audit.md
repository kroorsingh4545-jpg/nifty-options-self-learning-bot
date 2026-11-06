# Risk & Execution Audit Report

Date: 2026-11-06
Scope (files / PR): branch `upstox-analytics-token` vs `main` (b047e65 + 16701a9; 20 files, +455/−38):
`broker/upstox_rest.py`, `broker/errors.py`, `broker/factory.py`, `config/settings.py`, `config/loader.py`,
`config/__main__.py`, `core/enums.py`, `core/mode_controller.py`, `runtime/__main__.py`, `runtime/paper.py`,
`runtime/bot.py`, `pyproject.toml` (mypy pin), docs, `.env.example`, `.gitignore`; tests in
`tests/unit/test_analytics_token.py`, `tests/unit/test_upstox_rest.py`, `tests/unit/test_core_engine.py`,
`tests/conftest.py`. No changes in `risk/`, `execution/` or `strategies/`.

Mode audited: PAPER with an Upstox Analytics Token (`UPSTOX_TOKEN_TYPE=analytics`). LIVE order routing does not exist.
Environment: project `.venv` (Python 3.13.1), mypy 2.4.1 (pinned).
Tests: **1242 passed** (1 pytest warning, not inspected); ruff and mypy (strict, 133 files) clean.
Live check: `python -m runtime check --connect` PASS, one `GET /v3/market-quote/ltp` (HTTP 200), token never printed.
Verdict: **PASS WITH WARNINGS**

## Results

| # | Area | Status | Evidence (file:line, test) | Notes |
|---|---|---|---|---|
| 1 | Entry logic | N/A | not in diff | unchanged; full suite passes |
| 2 | Exit logic | N/A | not in diff | unchanged |
| 3 | Stop-loss logic | N/A | not in diff | unchanged |
| 4 | Take-profit logic | N/A | not in diff | unchanged |
| 5 | Trailing-stop logic | N/A | not in diff | unchanged |
| 6 | Position sizing | N/A | not in diff | unchanged |
| 7 | Daily loss limits | N/A | not in diff | unchanged |
| 8 | Global loss limits | N/A | not in diff | unchanged |
| 9 | Margin checks | N/A | `runtime/control.py:243` uses `bot.broker` (PaperBroker) | PAPER margin is simulated; never needs the funds API the analytics token can't call |
| 10 | Order lifecycle handling | N/A | not in diff | unchanged |
| 11 | Partial fills | N/A | not in diff | unchanged |
| 12 | Order rejection handling | N/A | not in diff | unchanged |
| 13 | Reconciliation | N/A | `execution/reconciliation.py:99-100` uses `self.broker` | PAPER reconciles against PaperBroker; no Upstox portfolio calls |
| 14 | Square-off logic | N/A | not in diff | unchanged |
| 15 | Kill-switch behavior | OK | `broker/upstox_rest.py:233` (scope check before `_token()`), `:211-215`; `test_analytics_token.py:103`, `:134` | A refused account call is not an auth failure, so it can't trip the kill switch; a real 401 still does |
| 16 | API rate-limit compliance | OK | `broker/upstox_rest.py:233` then `:238` `limiter.acquire`; `test_analytics_token.py:103` (`granted == 0`) | Refused calls use no rate-limit slot; every request still goes through the central limiter |
| 17 | PAPER/LIVE isolation | OK | `config/loader.py:101`; `broker/factory.py:33`; `core/mode_controller.py:86`; `broker/upstox_rest.py:417`; `runtime/bot.py:145`; `test_analytics_token.py:227`, `:236`, `:244`, `:258` | Four independent LIVE refusals for an analytics token; the config-load refusal runs before the existing "LIVE not in this build" check; default token type is the restrictive `analytics` |
| 18 | Restart recovery | N/A | not in diff | unchanged |
| 19 | Logging and audit trail | OK | `broker/upstox_rest.py:194` (logs the refused path only); `runtime/__main__.py:107-118`; `test_analytics_token.py:181` (secret absent from output) | `UPSTOX_TOKEN_TYPE` is not a secret; the token stays masked |

## Blocking issues

None.

## Warnings

1. **A daily token with the default setting loses the early expiry check.** With a daily token and
   `UPSTOX_TOKEN_TYPE` unset, the token is treated as `analytics`, which skips the 03:30 expiry check
   (`broker/upstox_rest.py:181-185`). An expired token is still caught by the first 401, which trips the kill
   switch (`AUTH_FAILURE`), so it fails safe; it is just detected later.
2. **Only the LTP endpoint has been verified live with the analytics token.** WebSocket authorise
   (`/v3/feed/market-data-feed/authorize`), option contract and intraday candle calls have not been. If Upstox
   rejects any of them with a 401, the session stops through the kill switch (`AUTH_FAILURE`): safe, but a real
   PAPER session is needed to confirm they work.
3. **No early warning before the analytics token's ~1-year expiry.** It is not tracked; expiry only shows up as
   a 401.
4. **The allowlist matches by path prefix** (`broker/upstox_rest.py:66-80`), and `/v2/option/contract` and
   `/v2/option/chain` have no trailing slash. All paths are built inside the client and instrument keys are
   URL-encoded (`:335`), so no way to reach a non-market-data endpoint was found; it is still looser than an
   exact match.

## Required tests missing

1. A real-market PAPER session (or WebSocket smoke test) with the analytics token, covering feed authorise,
   option contracts and candles.
2. A test that a daily token with `UPSTOX_TOKEN_TYPE` unset fails cleanly at the first 401 (warning 1).
