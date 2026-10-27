"""Report APIs as their providers return them: ads and analytics accounts, read a day at a time.

Unlike the file connectors (`data_sources.connectors`), a report source holds no configuration
of what to read: each call names the account and the report, and returns the provider's own
records with the days they cover (`RawReport`), for the application to store as it likes.

Every provider is a `ReportSource` subclass in a module under `data_sources/reports/sources/`,
found there by the registry: adding a provider is dropping in a file. Build one from a
`ConnectorConfig` (`provider` names it, `auth.credentials` holds the app's OAuth client,
`options` the rest):

    source = create_report_source(ConnectorConfig(provider="meta_ads", auth=AuthConfig(...)))
    url = source.authorize_url(state)
    grant = await source.exchange_code(code)           # Grant(credentials, identity, ...)
    accounts = await source.list_accounts(grant.credentials)
    report = await source.read(grant.credentials, accounts[0].id, spec, since, until)

Every source takes `transport` (an `httpx` transport, for tests) and `sleep` (what it waits
with between retries). Errors are `ReportError` (`ReportAuthError` when the grant is the
problem), with a `reason` an application can map to its own codes.
"""

from data_sources.reports.base import Account, Grant, ReportSource
from data_sources.reports.errors import Problem, ReportAuthError, ReportError
from data_sources.reports.google import GoogleReportSource
from data_sources.reports.models import RawReport, parse_day
from data_sources.reports.registry import (
    ReportSourceRegistry,
    create_report_source,
    discover,
    report_sources,
)

__all__ = [
    "Account",
    "Grant",
    "GoogleReportSource",
    "Problem",
    "RawReport",
    "ReportAuthError",
    "ReportError",
    "ReportSource",
    "ReportSourceRegistry",
    "create_report_source",
    "discover",
    "parse_day",
    "report_sources",
]
