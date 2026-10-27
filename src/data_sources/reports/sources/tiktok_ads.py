"""TikTok Ads, read-only, on Marketing API v1.3: advertiser authorization and daily BASIC reports.

Long-term Marketing API tokens, not TikTok organic-account tokens. The token exchange returns
every advertiser the user authorized; keep that list with the token, it bounds what may be read.
Credentials stay out of URLs and error messages.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from data_sources.core.logging import logger
from data_sources.reports.base import Account, Grant, ReportSource
from data_sources.reports.errors import Problem, ReportAuthError, ReportError
from data_sources.reports.http import ApiClient, Retry, Verdict
from data_sources.reports.models import RawReport, parse_day
from data_sources.reports.oauth import consent_url
from data_sources.reports.windows import windows

API_BASE = "https://business-api.tiktok.com/open_api/v1.3/"
AUTH_CODES = {40001, 40102, 40104, 40105, 40106, *range(40118, 40126)}
RETRY_CODES = {40100, 40016, 40133}
REPORT_WINDOW_DAYS = 30
PAGE_SIZE = 1000

UNREACHABLE = Problem("unavailable", "TikTok API could not be reached.")
BUSY = Problem("unavailable", "TikTok is temporarily unavailable or rate limited; retry later.")
INVALID = Problem("unavailable", "TikTok returned an invalid response.")
AUTH_FAILED = Problem(
    "auth_failed", "TikTok refused the token or advertiser permission; authorize again."
)
REFUSED = Problem(
    "request_failed", "TikTok refused the API request; check the app's reporting permissions."
)

_logger = logger.getChild("tiktok_ads.client")


def _fail(message: str, reason: str = "unavailable") -> ReportError:
    return Problem(reason, message).error()


@dataclass(frozen=True)
class Advertiser:
    account: str
    name: str
    currency: str
    timezone: str


def advertiser_ids(values: Any) -> list[str]:
    """The authorized advertiser ids, validated (digits only, deduplicated, in order)."""
    if not isinstance(values, list) or any(
        isinstance(v, bool)
        or not isinstance(v, (str, int))
        or not str(v).isascii()
        or not str(v).isdigit()
        for v in values
    ):
        raise ReportAuthError(
            "auth_failed", "TikTok did not return a valid advertiser list; authorize again."
        )
    return list(dict.fromkeys(str(v) for v in values))


def classify(response: httpx.Response) -> Verdict:
    """TikTok signals API errors inside HTTP 200 responses, as a `code` other than 0."""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    code = payload.get("code") if isinstance(payload, dict) else None
    if not isinstance(code, int) or isinstance(code, bool):
        code = None
    if response.status_code == 429 or response.status_code >= 500 or code in RETRY_CODES:
        return Retry(BUSY)
    if response.status_code in {401, 403} or code in AUTH_CODES:
        return AUTH_FAILED
    if not response.is_success or code != 0:
        return INVALID if code is None else REFUSED
    return None


class TikTokAdsReports(ReportSource):
    """TikTok Ads (`provider: tiktok_ads`): `app_id`, `app_secret` and `redirect_uri` in
    `auth.credentials`. The grant's credentials are `{"access_token", "advertiser_ids"}`; an
    account is an advertiser id (one of those); a report spec is `{"data_level", "dimensions",
    "metrics"}`."""

    provider = "tiktok_ads"
    app_credentials = ("app_id", "app_secret")

    def _client(self, token: str | None = None) -> ApiClient:
        return ApiClient(
            classify,
            unreachable=UNREACHABLE,
            invalid=INVALID,
            base_url=API_BASE,
            headers={"Access-Token": token} if token else None,
            transport=self.transport,
            sleep=self.sleep,
        )

    async def _data(
        self, api: ApiClient, method: str, endpoint: str, **kwargs: Any
    ) -> dict[str, Any]:
        data = (await api.json(method, endpoint, **kwargs)).get("data")
        if not isinstance(data, dict):
            raise INVALID.error()
        return data

    def authorize_url(self, state: str) -> str:
        return consent_url(
            "https://business-api.tiktok.com/portal/auth",
            app_id=self.credential("app_id"),
            redirect_uri=self.credential("redirect_uri"),
            state=state,
        )

    async def exchange_code(self, code: str) -> Grant:
        """The long-term token, and every advertiser it may read, the consent's code buys."""
        async with self._client() as api:
            try:
                data = await self._data(
                    api,
                    "POST",
                    "oauth2/access_token/",
                    json={
                        "app_id": self.credential("app_id"),
                        "secret": self.credential("app_secret"),
                        "auth_code": code,
                        "return_advertiser_ids": True,
                    },
                )
            except ReportError as exc:
                if exc.reason == "request_failed":
                    raise _fail("TikTok refused the authorization code.", "auth_failed") from exc
                raise
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise _fail("TikTok did not return an advertiser access token.", "auth_failed")
        return Grant(
            {"access_token": token, "advertiser_ids": advertiser_ids(data.get("advertiser_ids"))}
        )

    @staticmethod
    def _token(access_token: object) -> str:
        if not isinstance(access_token, str) or not access_token:
            raise _fail("The source has no TikTok access token.", "auth_failed")
        return access_token

    async def list_advertisers(self, access_token: str, authorized: Any) -> list[Advertiser]:
        """The authorized advertisers, with their names, currencies and timezones.

        Uses the id list from the token exchange instead of `oauth2/advertiser/get`, which takes
        the app secret in its query string (where HTTP and proxy logs would keep it)."""
        ids = advertiser_ids(authorized)
        found: dict[str, Advertiser] = {}
        async with self._client(self._token(access_token)) as api:
            for offset in range(0, len(ids), 10):
                wanted = ids[offset : offset + 10]
                data = await self._data(
                    api,
                    "GET",
                    "advertiser/info/",
                    params={
                        "advertiser_ids": json.dumps(wanted),
                        "fields": json.dumps(["advertiser_id", "name", "currency", "timezone"]),
                    },
                )
                items = data.get("list")
                if not isinstance(items, list):
                    raise _fail("TikTok returned an invalid advertiser list.")
                for item in items:
                    if not isinstance(item, dict) or str(item.get("advertiser_id")) not in wanted:
                        raise _fail("TikTok returned an unexpected advertiser.")
                    account = str(item["advertiser_id"])
                    found[account] = Advertiser(
                        account,
                        str(item.get("name") or item.get("advertiser_name") or account),
                        str(item.get("currency") or ""),
                        str(item.get("timezone") or ""),
                    )
        return [found[account] for account in ids if account in found]

    async def list_accounts(self, credentials: Mapping[str, Any]) -> list[Account]:
        return [
            Account(id=a.account, name=a.name, currency=a.currency, timezone=a.timezone)
            for a in await self.list_advertisers(
                credentials.get("access_token"),  # type: ignore[arg-type]
                credentials.get("advertiser_ids"),
            )
        ]

    async def read(
        self,
        credentials: Mapping[str, Any],
        account: str,
        spec: Mapping[str, Any],
        since: date,
        until: date,
    ) -> RawReport:
        return await self.read_report(
            credentials.get("access_token"),  # type: ignore[arg-type]
            credentials.get("advertiser_ids"),
            account,
            data_level=spec["data_level"],
            dimensions=list(spec["dimensions"]),
            metrics=list(spec["metrics"]),
            since=since,
            until=until,
        )

    async def read_report(
        self,
        access_token: str,
        authorized: Any,
        advertiser_id: str,
        *,
        data_level: str,
        dimensions: list[str],
        metrics: list[str],
        since: date,
        until: date,
    ) -> RawReport:
        """A daily BASIC auction report over `since..until` (inclusive), every entity status
        included, as TikTok returned its items. `dimensions` is the entity id and
        `stat_time_day`."""
        account = str(advertiser_id)
        if account not in advertiser_ids(authorized):
            raise _fail("This advertiser was not authorized for the saved token.", "auth_failed")
        records: list[dict[str, Any]] = []
        days: list[date | None] = []
        entity_id = next(d for d in dimensions if d != "stat_time_day")
        async with self._client(self._token(access_token)) as api:
            for start, end in windows(since, until, REPORT_WINDOW_DAYS):
                page = 1
                while True:
                    data = await self._data(
                        api,
                        "GET",
                        "report/integrated/get/",
                        params={
                            "advertiser_id": account,
                            "report_type": "BASIC",
                            "service_type": "AUCTION",
                            "data_level": data_level,
                            "dimensions": json.dumps(dimensions),
                            "metrics": json.dumps(metrics),
                            "filtering": json.dumps(
                                [
                                    {
                                        "field_name": entity_id.removesuffix("_id") + "_status",
                                        "filter_type": "IN",
                                        "filter_value": json.dumps(["STATUS_ALL"]),
                                    }
                                ]
                            ),
                            "start_date": start.isoformat(),
                            "end_date": end.isoformat(),
                            "page": page,
                            "page_size": PAGE_SIZE,
                        },
                    )
                    items = data.get("list")
                    if not isinstance(items, list):
                        raise _fail("TikTok returned an invalid report.")
                    for item in items:
                        found_dimensions = (
                            item.get("dimensions") if isinstance(item, dict) else None
                        )
                        found_metrics = item.get("metrics") if isinstance(item, dict) else None
                        day = (
                            parse_day(str(found_dimensions.get("stat_time_day", ""))[:10])
                            if isinstance(found_dimensions, dict)
                            else None
                        )
                        if (
                            day is None
                            or not start <= day <= end
                            or not isinstance(found_metrics, dict)
                            or not isinstance(found_dimensions, dict)
                            or not found_dimensions.get(entity_id)
                        ):
                            raise _fail("TikTok returned an invalid daily report row.")
                        records.append(item)
                        days.append(day)
                    info = data.get("page_info")
                    total = info.get("total_page") if isinstance(info, dict) else None
                    if total is None and not items:
                        break
                    if (
                        not isinstance(total, int)
                        or isinstance(total, bool)
                        or total < 0
                        or (items and total < page)
                        or (isinstance(info, dict) and info.get("page", page) != page)
                        or (total > page and not items)
                    ):
                        raise _fail("TikTok returned invalid report pagination.")
                    if page >= total:
                        break
                    page += 1
                    if page > 10000:
                        raise _fail(
                            "TikTok report is too large for a synchronous import.",
                            "request_failed",
                        )
        _logger.info(f"Read {len(records)} {data_level} rows for advertiser {account}")
        return RawReport(records, days, {"advertiser_id": account})
