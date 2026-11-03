"""Microsoft Clarity through its Data Export API (`project-live-insights`).

There is no OAuth: a project admin generates an API token in Clarity (Settings -> Data Export ->
Generate new API token), and that token is the grant. It reads one project, the one it was made
in, so there is one account and nothing to pick.

The API only gives totals over the last 24, 48 or 72 hours before the call (`numOfDays` 1, 2 or
3), broken down by up to three dimensions, in UTC. There is no per-day series and no history: a
report is a snapshot labelled with the day it was read, and an application that reads it every day
builds the history. Clarity allows **10 requests per project per day** (`429` past that, until the
UTC day ends) and at most 1,000 rows per response, without paging. A refused request may count
too, so a `429` is never retried.

Docs: https://learn.microsoft.com/en-us/clarity/setup-and-installation/clarity-data-export-api
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

import httpx

from data_sources.core.logging import logger
from data_sources.reports.base import Account, Grant, ReportSource
from data_sources.reports.errors import Problem, ReportError
from data_sources.reports.http import ApiClient, Retry, Verdict
from data_sources.reports.models import RawReport

API_URL = "https://www.clarity.ms/export-data/api/v1/project-live-insights"

#: What `dimension1..3` accept, spelled as Clarity spells them (they are also the record keys).
DIMENSIONS = (
    "Browser",
    "Device",
    "Country/Region",
    "OS",
    "Source",
    "Medium",
    "Campaign",
    "Channel",
    "URL",
)
MAX_DIMENSIONS = 3
DAY_SPANS = (1, 2, 3)
#: Clarity's cap on one response's rows (per metric): a metric that reaches it may be cut short.
ROW_LIMIT = 1000

UNREACHABLE = Problem("unavailable", "Microsoft Clarity could not be reached.")
BUSY = Problem("unavailable", "Microsoft Clarity is temporarily unavailable; try again later.")
INVALID = Problem("unavailable", "Microsoft Clarity returned an invalid response.")
RATE_LIMITED = Problem(
    "rate_limited",
    "Microsoft Clarity's limit of 10 requests per project per day was reached; "
    "try again after midnight UTC.",
)
AUTH_FAILED = Problem(
    "auth_failed",
    "Microsoft Clarity refused the API token: it is missing, invalid, expired or was replaced.",
)
NOT_OAUTH = Problem("request_failed", "Microsoft Clarity connects with an API token, not OAuth.")

_logger = logger.getChild("microsoft_clarity.reports")


def _today() -> date:
    return datetime.now(UTC).date()


def _said(response: httpx.Response) -> str:
    """What Clarity said about a refused request, short and on one line (never the token: it
    is only ever in the request's header)."""
    return " ".join(response.text.split())[:300]


def classify(response: httpx.Response) -> Verdict:
    """401/403: the token. 429: the daily limit, not retried (asking again only spends the next
    day's quota). 5xx: retried. Any other error: the request itself (`400`, bad parameters)."""
    if response.status_code == 429:
        return RATE_LIMITED
    if response.status_code >= 500:
        return Retry(BUSY)
    if response.status_code in {401, 403}:
        return AUTH_FAILED
    if response.is_error:
        return Problem(
            "request_failed",
            "Microsoft Clarity refused the request.",
            f"HTTP {response.status_code}: {_said(response)}",
        )
    return None


def query(spec: Mapping[str, Any]) -> dict[str, str]:
    """The request's parameters for a spec `{"dimensions": [...], "num_of_days": 1}`; a spec
    Clarity would refuse is refused here, before a request of the day's ten is spent on it."""
    dimensions = list(spec.get("dimensions") or [])
    if len(dimensions) > MAX_DIMENSIONS or len(set(dimensions)) != len(dimensions):
        raise ReportError("request_failed", "Clarity takes up to three different dimensions.")
    unknown = [d for d in dimensions if d not in DIMENSIONS]
    if unknown:
        raise ReportError("request_failed", f"Unknown Clarity dimension: {', '.join(unknown)}.")
    days = spec.get("num_of_days", 1)
    if days not in DAY_SPANS or isinstance(days, bool):
        raise ReportError("request_failed", "Clarity's numOfDays is 1, 2 or 3.")
    params = {"numOfDays": str(days)}
    for n, dimension in enumerate(dimensions, start=1):
        params[f"dimension{n}"] = dimension
    return params


def records_of(body: Any) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Clarity's `[{metricName, information: [row, ...]}, ...]` as one record per row, its metric
    named in it (`{"metricName": "Traffic", **row}`), and how many rows each metric had."""
    if not isinstance(body, list):
        raise INVALID.error()
    records: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for block in body:
        if not isinstance(block, dict) or not isinstance(block.get("metricName"), str):
            raise INVALID.error()
        rows = block.get("information") or []
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise INVALID.error()
        name = block["metricName"]
        counts[name] = counts.get(name, 0) + len(rows)
        records.extend({**row, "metricName": name} for row in rows)
    return records, counts


class MicrosoftClarityReports(ReportSource):
    """Microsoft Clarity (`provider: microsoft_clarity`): no app credentials. The grant's
    credentials are `{"api_token"}`, made by a project admin in Clarity; the one account is the
    token's project (`project`); a report spec is `{"dimensions": [...up to 3 of DIMENSIONS],
    "num_of_days": 1|2|3}`, read as of now (see the module's docstring)."""

    provider = "microsoft_clarity"

    def _client(self, token: str) -> ApiClient:
        return ApiClient(
            classify,
            unreachable=UNREACHABLE,
            invalid=INVALID,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            # A retry may be counted against the ten a day: one more try at most.
            attempts=2,
            transport=self.transport,
            sleep=self.sleep,
        )

    # No consent screen: the token is pasted.

    def authorize_url(self, state: str) -> str:
        raise NOT_OAUTH.error()

    async def exchange_code(self, code: str) -> Grant:
        raise NOT_OAUTH.error()

    @staticmethod
    def _token(credentials: Mapping[str, Any]) -> str:
        token = credentials.get("api_token")
        if not isinstance(token, str) or not token.strip():
            raise AUTH_FAILED.error()
        return token.strip()

    async def list_accounts(self, credentials: Mapping[str, Any]) -> list[Account]:
        """The token's project. Not asked of Clarity: every request spends one of the ten a day,
        and the API has no call that names the project."""
        self._token(credentials)
        return [Account(id="project", name="Clarity project")]

    async def read(
        self,
        credentials: Mapping[str, Any],
        account: str,
        spec: Mapping[str, Any],
        since: date,
        until: date,
    ) -> RawReport:
        """The totals over the last `num_of_days` × 24 hours, by `dimensions`, every record
        labelled with today (UTC). `since` and `until` don't apply: Clarity keeps no history the
        API can reach. One request."""
        params = query(spec)
        read_at = datetime.now(UTC)
        async with self._client(self._token(credentials)) as api:
            response = await api.request("GET", API_URL, params=params)
        try:
            body = response.json()
        except ValueError:
            raise INVALID.error() from None
        records, counts = records_of(body)
        _logger.info(
            f"Read {len(records)} Clarity rows ({', '.join(params.values())}) for {account}"
        )
        envelope = {
            "project": account,
            "dimensions": [params[k] for k in sorted(params) if k.startswith("dimension")],
            "num_of_days": int(params["numOfDays"]),
            "read_at": read_at.isoformat(),
            "metrics": counts,
            # A metric at Clarity's cap may have had more rows than it sent.
            "truncated": sorted(name for name, n in counts.items() if n >= ROW_LIMIT),
        }
        today = _today()
        return RawReport(records, [today] * len(records), envelope)
