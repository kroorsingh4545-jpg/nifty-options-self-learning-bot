"""Broker-layer exceptions. Messages never include credentials."""

from __future__ import annotations


class BrokerError(RuntimeError):
    """Base class for broker-layer failures."""


class LiveTradingDisabledError(BrokerError):
    """A real order action was attempted while LIVE routing is not permitted."""


class AuthError(BrokerError):
    """Credentials missing or rejected. Trading actions must stop; re-auth required."""


class TokenExpiredError(AuthError):
    """Access token expired or invalid (HTTP 401 / UDAPI100050 / daily expiry)."""


class TokenScopeError(BrokerError):
    """The configured token may not call this endpoint (an analytics token
    reaches market-data APIs only). Refused locally; nothing was sent."""


class RateLimitError(BrokerError):
    """Could not obtain a rate-limit slot within the allowed wait."""


class CircuitOpenError(RateLimitError):
    """The HTTP-429 circuit breaker is open for this API category."""


class BrokerTimeoutError(BrokerError):
    """Request timed out. For order requests the outcome is UNKNOWN and must be
    reconciled before any retry."""


class MaintenanceWindowError(BrokerError):
    """Upstox service is in its scheduled maintenance window (UDAPI100072)."""


class BrokerAPIError(BrokerError):
    """Upstox returned an error response."""

    def __init__(self, status_code: int, error_code: str | None, message: str) -> None:
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(f"HTTP {status_code} {error_code or ''}: {message}".strip())


class OrderRejectedError(BrokerError):
    """An order was rejected (paper simulation or broker)."""


class ConnectionLimitError(BrokerError):
    """A second market-data or portfolio WebSocket was requested (one of each allowed)."""
