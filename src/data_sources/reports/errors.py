from __future__ import annotations

from dataclasses import dataclass

from data_sources.core.exceptions import AuthenticationError, DataSourceError


class ReportError(DataSourceError):
    """A report API refused a request or couldn't be reached.

    `reason` says why, in a form an application can map to its own codes: `unavailable`
    (network, rate limit, 5xx, an invalid response), `auth_failed` (the grant is bad, expired
    or lacks a permission), `request_failed` (the provider refused the request itself),
    `oauth_not_configured`, `no_refresh_token`, `access_denied` (a consent without the scope
    asked for), `customer_not_enabled` (Google Ads). The message never holds a credential or
    the provider's raw response. `diagnostics` (when the provider says anything structured) is
    what it said, as its own error codes and messages with credentials removed: for logs and
    run timelines, not for end users.
    """

    def __init__(self, reason: str, message: str = "", diagnostics: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason
        self.message = message
        self.diagnostics = diagnostics

    @property
    def auth(self) -> bool:
        """Whether the grant itself is the problem: the user has to authorize again."""
        return isinstance(self, AuthenticationError)


class ReportAuthError(ReportError, AuthenticationError):
    """A `ReportError` caused by the grant (`reason` is `auth_failed`)."""


@dataclass(frozen=True)
class Problem:
    """An error kept as data, so the same one can be raised from several places."""

    reason: str
    message: str = ""
    diagnostics: str = ""

    def error(self) -> ReportError:
        cls = ReportAuthError if self.reason == "auth_failed" else ReportError
        return cls(self.reason, self.message, self.diagnostics)
