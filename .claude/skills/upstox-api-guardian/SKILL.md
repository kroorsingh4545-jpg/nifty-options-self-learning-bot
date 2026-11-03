---
name: upstox-api-guardian
description: Use whenever implementing, reviewing, or debugging Upstox API integration — REST clients, WebSocket market data, authentication and tokens, rate limiting, order placement code, error handling, and PAPER/LIVE separation in the broker layer.
---

# Upstox API Guardian

Apply this Skill to any code in `broker/` or `execution/`, or anything that talks to Upstox.

## 1. Official APIs only

- Use only the official Upstox Developer API (REST v2/v3 and the official market-data WebSocket feed).
- No scraping, no unofficial wrappers, no browser automation of the Upstox website.
- Check the current official Upstox documentation before implementing an endpoint — endpoints and limits change.

## 2. Central rate limiter (mandatory)

All REST requests go through **one** central rate limiter in `broker/`. No module may call `requests`/`httpx` against Upstox directly.

Official limits (from project spec — re-verify against Upstox docs before LIVE):

| API category | Per second | Per minute | Per 30 minutes |
|---|---|---|---|
| Order APIs | 10 | 500 | 2,000 |
| Other standard APIs | 50 | 500 | 2,000 |

Apply a **20% safety buffer**. The limiter must enforce these effective limits:

| API category | Per second | Per minute | Per 30 minutes |
|---|---|---|---|
| Order APIs | 8 | 400 | 1,600 |
| Other standard APIs | 40 | 400 | 1,600 |

- Enforce all three windows at once (sliding window or token buckets).
- Expose current usage per window as a metric for the dashboard.
- When a request would exceed a limit, queue it or reject it — never send it.

## 3. Market data

- The WebSocket feed is the **primary** live-data source.
- Do not poll quote / LTP REST APIs aggressively. REST quotes are only for occasional snapshots or recovery.
- Track subscribed instruments so they can be re-subscribed after reconnect.

## 4. Failure handling

Code must handle all of these explicitly:
- **Token expiry** — detect, stop trading actions, alert, require re-auth. Never loop on failed auth.
- **WebSocket disconnects** — reconnect with exponential backoff and jitter.
- **Reconnects** — re-subscribe all instruments; mark data as stale until fresh ticks arrive.
- **HTTP 429** — back off, respect any retry-after, record the event.
- **Timeouts** — treat order-request timeouts as *unknown state*, not failure; reconcile before retrying.
- **Partial failures** — e.g. one leg of a multi-leg action fails; reconcile and alert.

## 5. Secrets

- API key, secret, and access token come only from environment variables / `.env`.
- Mask them in all logs, exceptions, and dashboard output (e.g. `abcd****wxyz`).
- Never print request headers containing the token.

## 6. PAPER mode

- In PAPER mode the injected broker is `PaperBroker`. It must be **impossible** for an order to reach Upstox's order endpoints.
- Enforce with two independent checks: (a) the DI container wires `PaperBroker` in PAPER mode, and (b) the real order client refuses to send if `TRADING_MODE != LIVE`.

## 7. Approval checklist for order-related code

Do not approve order-related code until every item is verified:

- [ ] **Idempotency** — each order has a unique client tag; re-sending cannot create duplicates.
- [ ] **Retry safety** — retries only after confirming the previous attempt did not go through.
- [ ] **Reconciliation** — local orders/positions are reconciled with the broker on start, after errors, and periodically.
- [ ] **Kill-switch integration** — kill switch blocks new orders and triggers exits.
- [ ] **Rate-limit compliance** — all calls pass through the central limiter with the 20% buffer.
- [ ] **PAPER/LIVE isolation** — no path from PAPER mode to the real order API.

Report any unchecked item as a blocking issue.
