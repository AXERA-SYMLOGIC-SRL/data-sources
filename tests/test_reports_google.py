"""The Google report clients (Analytics, Search Console, Ads) against `httpx.MockTransport`, and
Google Ads' redacted error log."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any
from urllib.parse import parse_qs, quote, quote_plus

import httpx
import pytest

from data_sources.config.schema import AuthConfig, ConnectorConfig
from data_sources.reports import ReportAuthError, ReportError, create_report_source
from data_sources.reports.google_ads_errors import google_ads_error_codes, log_google_ads_error
from data_sources.reports.oauth import GOOGLE_TOKEN_URL
from data_sources.reports.sources import google_analytics as ga
from data_sources.reports.sources import search_console as sc
from data_sources.reports.sources.google_ads import Customer, GoogleAdsReports
from data_sources.reports.sources.google_analytics import (
    FieldInfo,
    GoogleAnalyticsReports,
    PropertySummary,
)
from data_sources.reports.sources.search_console import SearchConsoleReports, SearchConsoleSite

REFRESH = "private-refresh-token"
ACCESS = "private-access-token"
CLIENT_SECRET = "private-client-secret"
DEVELOPER_TOKEN = "private-developer-token"
PROPERTY = "properties/123456"
SITE = "sc-domain:example.com"
ACCOUNT = "1234567890"
MANAGER = "9876543210"
DISABLED = "5555555555"

Handler = Callable[[httpx.Request], httpx.Response]


class Sleeper:
    """A `sleep` that records how long it was asked to wait, and doesn't."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


@pytest.fixture
def sleep() -> Sleeper:
    return Sleeper()


def config(provider: str, **credentials: str) -> ConnectorConfig:
    """The app's Google OAuth client, as an application would configure the source."""
    return ConnectorConfig(
        provider=provider,
        auth=AuthConfig(
            type="oauth2",
            credentials={
                "client_id": "client-1",
                "client_secret": CLIENT_SECRET,
                "redirect_uri": "https://app.example.com/callback",
                **credentials,
            },
        ),
    )


def with_token(handler: Handler, requests: list[httpx.Request] | None = None) -> Handler:
    """Answer Google's token endpoint with `ACCESS`, and check every other request carries it."""

    def wrapped(request: httpx.Request) -> httpx.Response:
        if str(request.url) == GOOGLE_TOKEN_URL:
            assert parse_qs(request.content.decode())["refresh_token"] == [REFRESH]
            return httpx.Response(200, json={"access_token": ACCESS})
        assert request.headers["Authorization"] == f"Bearer {ACCESS}"
        assert "private-" not in str(request.url)
        if requests is not None:
            requests.append(request)
        return handler(request)

    return wrapped


def token_refused(request: httpx.Request) -> httpx.Response:
    if str(request.url) == GOOGLE_TOKEN_URL:
        return httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "Token has been revoked."}
        )
    raise AssertionError("no API call without an access token")


def body_of(request: httpx.Request) -> dict[str, Any]:
    body = json.loads(request.content)
    assert isinstance(body, dict)
    return body


# --- Google Analytics -----------------------------------------------------------------------


def analytics(handler: Handler, sleep: Sleeper) -> GoogleAnalyticsReports:
    source = create_report_source(
        config("google_analytics"), transport=httpx.MockTransport(handler), sleep=sleep
    )
    assert isinstance(source, GoogleAnalyticsReports)
    return source


async def test_analytics_lists_properties_over_admin_pages(sleep: Sleeper) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1beta/accountSummaries"
        assert request.url.params["pageSize"] == "200"
        if "pageToken" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "accountSummaries": [
                        {
                            "account": "accounts/1",
                            "displayName": "Shop",
                            "propertySummaries": [
                                {"property": "properties/11", "displayName": "Web"},
                                {"property": "properties/12"},
                            ],
                        },
                        {"account": "accounts/2", "displayName": "Empty"},
                    ],
                    "nextPageToken": "page-2",
                },
            )
        assert request.url.params["pageToken"] == "page-2"
        return httpx.Response(
            200,
            json={
                "accountSummaries": [
                    {
                        "account": "accounts/3",
                        "propertySummaries": [{"property": "properties/31", "displayName": "App"}],
                    }
                ]
            },
        )

    properties = await analytics(with_token(handler, seen), sleep).list_properties(REFRESH)
    assert properties == [
        PropertySummary("properties/11", "Web", "accounts/1", "Shop"),
        PropertySummary("properties/12", "", "accounts/1", "Shop"),
        PropertySummary("properties/31", "App", "accounts/3", ""),
    ]
    assert len(seen) == 2 and seen[0].url.host == "analyticsadmin.googleapis.com"


METADATA: dict[str, Any] = {
    "name": f"{PROPERTY}/metadata",
    "dimensions": [
        {"apiName": "date", "uiName": "Date", "description": "The day.", "category": "Time"},
        {
            "apiName": "customEvent:plan",
            "uiName": "Plan",
            "category": "Custom",
            "customDefinition": True,
        },
    ],
    "metrics": [
        {"apiName": "sessions", "uiName": "Sessions", "type": "TYPE_INTEGER"},
        {"apiName": "customEvent:price", "type": "TYPE_CURRENCY", "customDefinition": True},
    ],
}


def test_parse_fields_lists_dimensions_then_metrics() -> None:
    assert ga.parse_fields(METADATA) == [
        FieldInfo("dimension", "date", "Date", "The day.", "Time", False, None),
        FieldInfo("dimension", "customEvent:plan", "Plan", "", "Custom", True, None),
        FieldInfo("metric", "sessions", "Sessions", "", "", False, "TYPE_INTEGER"),
        FieldInfo("metric", "customEvent:price", "", "", "", True, "TYPE_CURRENCY"),
    ]
    assert ga.parse_fields({}) == []


async def test_analytics_lists_a_propertys_fields(sleep: Sleeper) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == f"{ga.DATA_URL}/{PROPERTY}/metadata"
        return httpx.Response(200, json=METADATA)

    fields = await analytics(with_token(handler), sleep).list_fields(REFRESH, PROPERTY)
    assert [(f.kind, f.api_name, f.custom) for f in fields] == [
        ("dimension", "date", False),
        ("dimension", "customEvent:plan", True),
        ("metric", "sessions", False),
        ("metric", "customEvent:price", True),
    ]


def ga_row(day: str, channel: str, sessions: int) -> dict[str, Any]:
    return {
        "dimensionValues": [{"value": channel}, {"value": day}],
        "metricValues": [{"value": str(sessions)}],
    }


GA_ROWS = [
    ga_row("20261001", "Organic", 3),
    ga_row("20261001", "Direct", 1),
    ga_row("20261002", "Organic", 4),
    ga_row("20261002", "Paid", 2),
    ga_row("(other)", "Other", 9),
]


def ga_pages(rows: list[dict[str, Any]], seen: list[dict[str, Any]]) -> Handler:
    """`runReport` serving `rows` by `offset`/`limit`, with headers and metadata on each page."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == f"{ga.DATA_URL}/{PROPERTY}:runReport"
        body = body_of(request)
        seen.append(body)
        page = rows[body["offset"] : body["offset"] + body["limit"]]
        return httpx.Response(
            200,
            json={
                "dimensionHeaders": [{"name": "sessionDefaultChannelGroup"}, {"name": "date"}],
                "metricHeaders": [{"name": "sessions", "type": "TYPE_INTEGER"}],
                **({"rows": page} if page else {}),
                "rowCount": len(rows),
                "metadata": {"currencyCode": "RON", "timeZone": "Europe/Bucharest"},
                "kind": "analyticsData#runReport",
            },
        )

    return handler


async def test_analytics_report_pages_by_offset_until_row_count(
    sleep: Sleeper, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ga, "PAGE_SIZE", 2)
    seen: list[dict[str, Any]] = []
    report = await analytics(with_token(ga_pages(GA_ROWS, seen)), sleep).read_report(
        REFRESH,
        PROPERTY,
        dimensions=["sessionDefaultChannelGroup", "date"],
        metrics=["sessions"],
        since=date(2026, 10, 1),
        until=date(2026, 10, 2),
    )
    assert [b["offset"] for b in seen] == [0, 2, 4]
    assert seen[0] == {
        "dimensions": [{"name": "sessionDefaultChannelGroup"}, {"name": "date"}],
        "metrics": [{"name": "sessions"}],
        "dateRanges": [{"startDate": "2026-10-01", "endDate": "2026-10-02"}],
        "limit": 2,
        "offset": 0,
    }
    assert report.records == GA_ROWS
    assert report.days == [
        date(2026, 10, 1),
        date(2026, 10, 1),
        date(2026, 10, 2),
        date(2026, 10, 2),
        None,
    ]
    assert report.envelope == {
        "dimensionHeaders": [{"name": "sessionDefaultChannelGroup"}, {"name": "date"}],
        "metricHeaders": [{"name": "sessions", "type": "TYPE_INTEGER"}],
        "rowCount": 5,
        "metadata": {"currencyCode": "RON", "timeZone": "Europe/Bucharest"},
        "kind": "analyticsData#runReport",
    }


async def test_analytics_report_without_a_day_column_has_no_days(sleep: Sleeper) -> None:
    rows = GA_ROWS[:2]
    report = await analytics(with_token(ga_pages(rows, [])), sleep).read_report(
        REFRESH,
        PROPERTY,
        dimensions=["sessionDefaultChannelGroup", "date"],
        metrics=["sessions"],
        since=date(2026, 10, 1),
        until=date(2026, 10, 1),
        day_column=None,
    )
    assert report.records == rows and report.days == [None, None]


async def test_analytics_report_stops_on_an_empty_page(sleep: Sleeper) -> None:
    seen: list[dict[str, Any]] = []
    report = await analytics(with_token(ga_pages([], seen)), sleep).read_report(
        REFRESH,
        PROPERTY,
        dimensions=["date"],
        metrics=["sessions"],
        since=date(2026, 10, 1),
        until=date(2026, 10, 1),
    )
    assert len(seen) == 1 and report.records == [] and report.days == []
    assert report.envelope["rowCount"] == 0


@pytest.mark.parametrize(("header", "wait"), [("7", 7.0), ("600", 60.0), (None, 1.0)])
async def test_analytics_report_honours_retry_after(
    sleep: Sleeper, header: str | None, wait: float
) -> None:
    rows: list[dict[str, Any]] = []
    calls: list[int] = []
    pages = ga_pages(GA_ROWS[:1], rows)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            headers = {"Retry-After": header} if header else {}
            return httpx.Response(429, headers=headers, json={"error": {"code": 429}})
        return pages(request)

    report = await analytics(with_token(handler), sleep).read_report(
        REFRESH,
        PROPERTY,
        dimensions=["sessionDefaultChannelGroup", "date"],
        metrics=["sessions"],
        since=date(2026, 10, 1),
        until=date(2026, 10, 1),
    )
    assert len(report.records) == 1
    assert sleep.waits == [wait]


async def test_analytics_gives_up_after_six_throttled_attempts(sleep: Sleeper) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429)

    with pytest.raises(ReportError) as caught:
        await analytics(with_token(handler), sleep).list_fields(REFRESH, PROPERTY)
    assert caught.value.reason == "unavailable" and caught.value.message == ga.BUSY.message
    assert len(calls) == 6 and sleep.waits == [1.0, 2.0, 4.0, 8.0, 16.0]


async def test_analytics_403_is_an_auth_error(sleep: Sleeper) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED"}})

    with pytest.raises(ReportAuthError) as caught:
        await analytics(with_token(handler), sleep).list_properties(REFRESH)
    assert caught.value.auth and caught.value.reason == "auth_failed"
    assert len(calls) == 1 and sleep.waits == []


async def test_analytics_refused_refresh_token_is_an_auth_error(sleep: Sleeper) -> None:
    with pytest.raises(ReportAuthError) as caught:
        await analytics(token_refused, sleep).list_properties(REFRESH)
    assert caught.value.reason == "auth_failed"
    assert "revoked" not in str(caught.value)


async def test_analytics_other_errors_are_unavailable(sleep: Sleeper) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "Field foo is not valid"}})

    with pytest.raises(ReportError) as caught:
        await analytics(with_token(handler), sleep).list_fields(REFRESH, PROPERTY)
    assert caught.value.reason == "unavailable" and not caught.value.auth
    assert "foo" not in str(caught.value)


@pytest.mark.parametrize("bad", ["123456", "properties/abc", "properties/1/x", ""])
async def test_analytics_refuses_an_invalid_property_before_asking(
    sleep: Sleeper, bad: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request for an invalid property")

    client = analytics(handler, sleep)
    with pytest.raises(ReportError) as caught:
        await client.list_fields(REFRESH, bad)
    assert caught.value.reason == "unavailable"
    with pytest.raises(ReportError):
        await client.read_report(
            REFRESH,
            bad,
            dimensions=["date"],
            metrics=["sessions"],
            since=date(2026, 10, 1),
            until=date(2026, 10, 1),
        )


async def test_analytics_consent_and_code_exchange(sleep: Sleeper) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"refresh_token": "new", "scope": ga.SCOPE})

    client = analytics(handler, sleep)
    assert parse_qs(client.authorize_url("s").split("?", 1)[1])["scope"] == [
        f"{ga.SCOPE} openid email"
    ]
    assert (await client.exchange_code("code")).credentials == {"refresh_token": "new"}


# --- Search Console -------------------------------------------------------------------------


def search_console(handler: Handler, sleep: Sleeper) -> SearchConsoleReports:
    source = create_report_source(
        config("google_search_console"), transport=httpx.MockTransport(handler), sleep=sleep
    )
    assert isinstance(source, SearchConsoleReports)
    return source


async def test_search_console_lists_readable_sites(sleep: Sleeper) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and str(request.url) == f"{sc.API_URL}/sites"
        return httpx.Response(
            200,
            json={
                "siteEntry": [
                    {"siteUrl": SITE, "permissionLevel": "siteOwner"},
                    {"siteUrl": "https://example.com/", "permissionLevel": "siteFullUser"},
                    {
                        "siteUrl": "https://shop.example.com/",
                        "permissionLevel": "siteRestrictedUser",
                    },
                    {"siteUrl": "https://other.example/", "permissionLevel": "siteUnverifiedUser"},
                ]
            },
        )

    sites = await search_console(with_token(handler), sleep).list_sites(REFRESH)
    assert sites == [
        SearchConsoleSite(SITE, "siteOwner"),
        SearchConsoleSite("https://example.com/", "siteFullUser"),
        SearchConsoleSite("https://shop.example.com/", "siteRestrictedUser"),
    ]


async def test_search_console_without_sites(sleep: Sleeper) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    assert await search_console(with_token(handler), sleep).list_sites(REFRESH) == []


@pytest.mark.parametrize(
    "entries",
    [
        "nope",
        [{"siteUrl": "", "permissionLevel": "siteOwner"}],
        [{"siteUrl": SITE}],
        ["site"],
    ],
)
async def test_search_console_invalid_sites(sleep: Sleeper, entries: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"siteEntry": entries})

    with pytest.raises(ReportError) as caught:
        await search_console(with_token(handler), sleep).list_sites(REFRESH)
    assert caught.value.reason == "unavailable"


def sc_row(day: date, query: str, clicks: int = 1) -> dict[str, Any]:
    return {
        "keys": [day.isoformat(), query],
        "clicks": clicks,
        "impressions": 10,
        "ctr": 0.1,
        "position": 3.5,
    }


def sc_days(rows_per_day: dict[date, int], seen: list[dict[str, Any]]) -> Handler:
    """Search Analytics serving `rows_per_day[day]` rows by `startRow`/`rowLimit`."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.raw_path.decode() == (
            "/webmasters/v3/sites/sc-domain%3Aexample.com/searchAnalytics/query"
        )
        body = body_of(request)
        seen.append(body)
        day = date.fromisoformat(body["startDate"])
        assert body["endDate"] == body["startDate"]
        total = [sc_row(day, f"q{n}") for n in range(rows_per_day.get(day, 0))]
        page = total[body["startRow"] : body["startRow"] + body["rowLimit"]]
        return httpx.Response(200, json={"rows": page} if page else {})

    return handler


async def test_search_console_reads_one_day_at_a_time(sleep: Sleeper) -> None:
    seen: list[dict[str, Any]] = []
    first, second, third = date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22)
    handler = sc_days({first: 2, third: 1}, seen)
    report = await search_console(with_token(handler), sleep).read_report(
        REFRESH, SITE, ["date", "query"], first, third
    )
    assert [b["startDate"] for b in seen] == [d.isoformat() for d in (first, second, third)]
    assert seen[0] == {
        "startDate": "2026-10-20",
        "endDate": "2026-10-20",
        "dimensions": ["date", "query"],
        "type": "web",
        "dataState": "all",
        "aggregationType": "byProperty",
        "rowLimit": sc.PAGE_SIZE,
        "startRow": 0,
    }
    assert report.records == [sc_row(first, "q0"), sc_row(first, "q1"), sc_row(third, "q0")]
    assert report.days == [first, first, third]
    assert report.envelope == {
        "siteUrl": SITE,
        "dimensions": ["date", "query"],
        "searchType": "web",
        "dataState": "all",
        "limitedDays": [],
    }


async def test_search_console_reads_the_search_type_it_is_asked_for(sleep: Sleeper) -> None:
    seen: list[dict[str, Any]] = []
    day = date(2026, 10, 20)
    report = await search_console(with_token(sc_days({day: 1}, seen)), sleep).read(
        {"refresh_token": REFRESH},
        SITE,
        {"dimensions": ["date", "page"], "search_type": "image"},
        day,
        day,
    )
    assert seen[0]["type"] == "image" and report.envelope["searchType"] == "image"


async def test_discover_and_news_rows_have_no_position(sleep: Sleeper) -> None:
    day = date(2026, 10, 20)

    def handler(request: httpx.Request) -> httpx.Response:
        row = {k: v for k, v in sc_row(day, "https://example.com/a").items() if k != "position"}
        return httpx.Response(200, json={"rows": [row]})

    source = search_console(with_token(handler), sleep)
    report = await source.read_report(
        REFRESH, SITE, ["date", "page"], day, day, search_type="discover"
    )
    assert report.days == [day] and "position" not in report.records[0]
    with pytest.raises(ReportError):  # a web row without a position is still invalid
        await source.read_report(REFRESH, SITE, ["date", "page"], day, day)


def refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError("no request expected")


async def test_an_unknown_search_type_never_reaches_google(sleep: Sleeper) -> None:
    day = date(2026, 10, 20)
    with pytest.raises(ReportError) as caught:
        await search_console(refuse, sleep).read_report(
            REFRESH, SITE, ["date", "page"], day, day, search_type="maps"
        )
    assert caught.value.reason == "request_failed"


async def test_search_console_aggregates_by_page_when_asked_for_pages(sleep: Sleeper) -> None:
    seen: list[dict[str, Any]] = []
    day = date(2026, 10, 20)
    await search_console(with_token(sc_days({}, seen)), sleep).read_report(
        REFRESH, SITE, ["date", "page"], day, day
    )
    assert seen[0]["aggregationType"] == "byPage"


async def test_search_console_pages_by_start_row_and_marks_limited_days(
    sleep: Sleeper, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sc, "PAGE_SIZE", 2)
    monkeypatch.setattr(sc, "DAILY_LIMIT", 4)
    seen: list[dict[str, Any]] = []
    days = [date(2026, 10, 20) + timedelta(days=n) for n in range(4)]
    # 3 rows: two pages; 2 rows: a full page then an empty one; 4 and 7 rows reach the limit.
    handler = sc_days({days[0]: 3, days[1]: 2, days[2]: 4, days[3]: 7}, seen)
    report = await search_console(with_token(handler), sleep).read_report(
        REFRESH, SITE, ["date", "query"], days[0], days[3]
    )
    assert [(b["startDate"][-2:], b["startRow"]) for b in seen] == [
        ("20", 0),
        ("20", 2),
        ("21", 0),
        ("21", 2),
        ("22", 0),
        ("22", 2),
        ("23", 0),
        ("23", 2),
    ]
    assert all(b["rowLimit"] == 2 for b in seen)
    assert report.days == [days[0]] * 3 + [days[1]] * 2 + [days[2]] * 4 + [days[3]] * 4
    assert report.envelope["limitedDays"] == [days[2].isoformat(), days[3].isoformat()]


async def test_search_console_quota_403_is_retried_with_linear_backoff(sleep: Sleeper) -> None:
    calls: list[int] = []
    day = date(2026, 10, 20)
    pages = sc_days({day: 1}, [])

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            errors = [{"reason": "quotaExceeded", "domain": "usageLimits"}]
            return httpx.Response(403, json={"error": {"code": 403, "errors": errors}})
        if len(calls) == 2:
            return httpx.Response(403, json={"error": {"status": "RESOURCE_EXHAUSTED"}})
        return pages(request)

    report = await search_console(with_token(handler), sleep).read_report(
        REFRESH, SITE, ["date", "query"], day, day
    )
    assert len(report.records) == 1
    assert len(calls) == 3 and sleep.waits == [1.0, 2.0]


async def test_search_console_rate_limit_runs_out(sleep: Sleeper) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    with pytest.raises(ReportError) as caught:
        await search_console(with_token(handler), sleep).list_sites(REFRESH)
    assert caught.value.reason == "unavailable" and caught.value.message == sc.BUSY.message
    assert sleep.waits == [1.0, 2.0]


async def test_search_console_permission_403_is_an_auth_error(sleep: Sleeper) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        errors = [{"reason": "forbidden"}]
        return httpx.Response(403, json={"error": {"code": 403, "errors": errors}})

    with pytest.raises(ReportAuthError):
        await search_console(with_token(handler), sleep).list_sites(REFRESH)
    assert len(calls) == 1 and sleep.waits == []


async def test_search_console_token_error_is_auth_failed(sleep: Sleeper) -> None:
    with pytest.raises(ReportAuthError) as caught:
        await search_console(token_refused, sleep).list_sites(REFRESH)
    assert caught.value.reason == "auth_failed" and caught.value.auth


async def test_search_console_other_errors_are_request_failed(sleep: Sleeper) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad dimension"}})

    day = date(2026, 10, 20)
    with pytest.raises(ReportError) as caught:
        await search_console(with_token(handler), sleep).read_report(
            REFRESH, SITE, ["date"], day, day
        )
    assert caught.value.reason == "request_failed" and "bad dimension" not in str(caught.value)


DAY = date(2026, 10, 20)


@pytest.mark.parametrize(
    "rows",
    [
        "rows",
        [{"keys": ["2026-10-20"], "clicks": 1, "impressions": 1, "ctr": 1, "position": 1}],
        [{**sc_row(DAY, "q"), "keys": ["2026-10-21", "q"]}],
        [{**sc_row(DAY, "q"), "keys": ["2026-10-20", 5]}],
        [{**sc_row(DAY, "q"), "clicks": "1"}],
        [{**sc_row(DAY, "q"), "position": None}],
        [{**sc_row(DAY, "q"), "ctr": True}],
        [{k: v for k, v in sc_row(DAY, "q").items() if k != "impressions"}],
        ["row"],
    ],
)
async def test_search_console_invalid_rows_are_unavailable(sleep: Sleeper, rows: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rows": rows})

    with pytest.raises(ReportError) as caught:
        await search_console(with_token(handler), sleep).read_report(
            REFRESH, SITE, ["date", "query"], DAY, DAY
        )
    assert caught.value.reason == "unavailable" and not caught.value.auth


async def test_search_console_more_rows_than_asked_for_is_invalid(
    sleep: Sleeper, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sc, "PAGE_SIZE", 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rows": [sc_row(DAY, "a"), sc_row(DAY, "b")]})

    with pytest.raises(ReportError) as caught:
        await search_console(with_token(handler), sleep).read_report(
            REFRESH, SITE, ["date", "query"], DAY, DAY
        )
    assert caught.value.reason == "unavailable"


@pytest.mark.parametrize(
    ("site", "dimensions", "since", "until"),
    [
        (SITE, ["query", "date"], DAY, DAY),
        (SITE, [], DAY, DAY),
        ("", ["date"], DAY, DAY),
        (SITE, ["date"], DAY, DAY - timedelta(days=1)),
    ],
)
async def test_search_console_bad_definition_is_request_failed(
    sleep: Sleeper, site: str, dimensions: list[str], since: date, until: date
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request for a bad definition")

    with pytest.raises(ReportError) as caught:
        await search_console(handler, sleep).read_report(REFRESH, site, dimensions, since, until)
    assert caught.value.reason == "request_failed"


# --- Google Ads -----------------------------------------------------------------------------


def ads(
    handler: Handler, sleep: Sleeper, developer_token: str = DEVELOPER_TOKEN
) -> GoogleAdsReports:
    source = create_report_source(
        ads_config(developer_token), transport=httpx.MockTransport(handler), sleep=sleep
    )
    assert isinstance(source, GoogleAdsReports)
    return source


def ads_config(developer_token: str = DEVELOPER_TOKEN, version: str = "v21") -> ConnectorConfig:
    base = config("google_ads", developer_token=developer_token)
    return base.model_copy(update={"options": {"api_version": version}})


def not_enabled() -> httpx.Response:
    return httpx.Response(
        403,
        json={
            "error": {
                "code": 403,
                "message": "The caller does not have permission",
                "status": "PERMISSION_DENIED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.ads.googleads.v21.errors."
                        "GoogleAdsFailure",
                        "errors": [
                            {
                                "errorCode": {"authorizationError": "CUSTOMER_NOT_ENABLED"},
                                "message": "The customer account can't be accessed.",
                            }
                        ],
                        "requestId": "req-1",
                    }
                ],
            }
        },
    )


async def test_ads_lists_direct_customers_and_a_managers_clients(sleep: Sleeper) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["developer-token"] == DEVELOPER_TOKEN
        if request.url.path == "/v21/customers:listAccessibleCustomers":
            assert request.method == "GET" and "login-customer-id" not in request.headers
            names = [f"customers/{MANAGER}", f"customers/{ACCOUNT}", f"customers/{DISABLED}"]
            return httpx.Response(200, json={"resourceNames": names})
        query = body_of(request)["query"]
        root = request.url.path.split("/")[3].split(":")[0]
        assert request.url.path == f"/v21/customers/{root}/googleAds:search"
        if root == DISABLED:
            return not_enabled()
        if "FROM customer_client" in query:
            assert root == MANAGER and request.headers["login-customer-id"] == MANAGER
            assert "customer_client.status = 'ENABLED'" in query
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "customerClient": {
                                "id": ACCOUNT,
                                "descriptiveName": "Shop",
                                "currencyCode": "RON",
                                "timeZone": "Europe/Bucharest",
                            }
                        },
                        {"customerClient": {"id": "2222222222", "descriptiveName": "Other"}},
                    ]
                },
            )
        assert "login-customer-id" not in request.headers
        detail = {
            "id": root,
            "descriptiveName": "Agency" if root == MANAGER else "Shop direct",
            "currencyCode": "EUR",
            "timeZone": "Europe/Paris",
            "manager": root == MANAGER,
        }
        return httpx.Response(200, json={"results": [{"customer": detail}]})

    customers = await ads(with_token(handler, seen), sleep).list_customers(REFRESH)
    assert customers == [
        Customer(ACCOUNT, "Shop", "RON", "Europe/Bucharest", MANAGER),
        Customer("2222222222", "Other", "", "", MANAGER),
        Customer(ACCOUNT, "Shop direct", "EUR", "Europe/Paris", ""),
    ]
    assert len(seen) == 5 and sleep.waits == []


async def test_ads_without_a_developer_token_sends_none(sleep: Sleeper) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "developer-token" not in request.headers
        return httpx.Response(200, json={"resourceNames": []})

    assert await ads(with_token(handler), sleep, developer_token="").list_customers(REFRESH) == []


@pytest.mark.parametrize(
    "body",
    [
        {"resourceNames": "customers/1234567890"},
        {"resourceNames": ["customers/123"]},
        {"resourceNames": ["accounts/1234567890"]},
    ],
)
async def test_ads_invalid_customer_list(sleep: Sleeper, body: dict[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(ReportError) as caught:
        await ads(with_token(handler), sleep).list_customers(REFRESH)
    assert caught.value.reason == "unavailable"


async def test_ads_other_errors_on_a_customer_are_not_skipped(sleep: Sleeper) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(":listAccessibleCustomers"):
            return httpx.Response(200, json={"resourceNames": [f"customers/{ACCOUNT}"]})
        return httpx.Response(403, json={"error": {"code": 403, "status": "PERMISSION_DENIED"}})

    with pytest.raises(ReportAuthError) as caught:
        await ads(with_token(handler), sleep).list_customers(REFRESH)
    assert caught.value.reason == "auth_failed"


async def test_ads_rate_limit_is_retried(sleep: Sleeper) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, json={})
        return httpx.Response(200, json={"resourceNames": []})

    assert await ads(with_token(handler), sleep).list_customers(REFRESH) == []
    assert len(calls) == 3 and sleep.waits == [0.5, 1.0]


def ads_row(day: str = "2026-10-01", account: str = ACCOUNT, **extra: Any) -> dict[str, Any]:
    return {
        "customer": {"id": account, "currencyCode": "RON"},
        "campaign": {"id": "77", "name": "Shop"},
        "segments": {"date": day},
        "metrics": {"costMicros": "1234567", "clicks": "5"},
        **extra,
    }


CAMPAIGN_FIELDS = ["customer.id", "segments.date", "campaign.id", "metrics.clicks"]


async def read_ads(
    handler: Handler,
    sleep: Sleeper,
    *,
    resource: str = "campaign",
    fields: list[str] | None = None,
    since: date = date(2026, 10, 1),
    until: date = date(2026, 10, 2),
    customer: str = ACCOUNT,
    login: str = MANAGER,
) -> Any:
    return await ads(with_token(handler), sleep).read_report(
        REFRESH,
        customer,
        login,
        resource=resource,
        fields=fields or CAMPAIGN_FIELDS,
        since=since,
        until=until,
    )


async def test_ads_report_splits_into_30_day_windows_and_follows_pages(sleep: Sleeper) -> None:
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v21/customers/{ACCOUNT}/googleAds:search"
        assert request.headers["login-customer-id"] == MANAGER
        assert request.headers["developer-token"] == DEVELOPER_TOKEN
        body = body_of(request)
        window = body["query"].split("BETWEEN ", 1)[1]
        seen.append((window, body.get("pageToken")))
        start = window.split("'")[1]
        if start == "2026-08-01" and "pageToken" not in body:
            return httpx.Response(200, json={"results": [ads_row(start)], "nextPageToken": "p2"})
        return httpx.Response(200, json={"results": [ads_row(start)]})

    report = await read_ads(handler, sleep, since=date(2026, 8, 1), until=date(2026, 10, 1))
    assert seen == [
        ("'2026-08-01' AND '2026-08-30'", None),
        ("'2026-08-01' AND '2026-08-30'", "p2"),
        ("'2026-08-31' AND '2026-09-29'", None),
        ("'2026-09-30' AND '2026-10-01'", None),
    ]
    assert report.days == [date(2026, 8, 1), date(2026, 8, 1), date(2026, 8, 31), date(2026, 9, 30)]
    assert report.records[0]["metrics"]["costMicros"] == "1234567"
    assert report.envelope == {"customer_id": ACCOUNT, "login_customer_id": MANAGER}


async def test_ads_report_query_names_fields_and_resource(sleep: Sleeper) -> None:
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert "login-customer-id" not in request.headers
        queries.append(body_of(request)["query"])
        return httpx.Response(200, json={})

    report = await read_ads(handler, sleep, login="")
    assert queries == [
        "SELECT customer.id, segments.date, campaign.id, metrics.clicks FROM campaign "
        "WHERE segments.date BETWEEN '2026-10-01' AND '2026-10-02'"
    ]
    assert report.records == [] and report.envelope["login_customer_id"] == ""


async def test_ads_report_checks_finer_resources_ids(sleep: Sleeper) -> None:
    good_ad = ads_row(adGroup={"id": "88"}, adGroupAd={"ad": {"id": "99"}})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [good_ad]})

    report = await read_ads(handler, sleep, resource="ad_group_ad")
    assert report.records == [good_ad]


@pytest.mark.parametrize(
    ("resource", "row"),
    [
        ("campaign", ads_row("2026-09-30")),
        ("campaign", ads_row("2026-10-03")),
        ("campaign", ads_row("not-a-day")),
        ("campaign", ads_row(account="1111111111")),
        ("campaign", {k: v for k, v in ads_row().items() if k != "metrics"}),
        ("campaign", ads_row(campaign={"name": "no id"})),
        ("campaign", {"metrics": {}}),
        ("ad_group", ads_row()),
        ("ad_group", ads_row(adGroup={})),
        ("ad_group_ad", ads_row(adGroup={"id": "88"})),
        ("ad_group_ad", ads_row(adGroup={"id": "88"}, adGroupAd={"ad": {}})),
    ],
)
async def test_ads_report_refuses_invalid_rows(
    sleep: Sleeper, resource: str, row: dict[str, Any]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [row]})

    with pytest.raises(ReportError) as caught:
        await read_ads(handler, sleep, resource=resource)
    assert caught.value.reason == "unavailable"


@pytest.mark.parametrize(
    "body",
    [
        {"results": "rows"},
        {"results": ["row"]},
        {"results": [], "nextPageToken": "more"},
    ],
)
async def test_ads_report_refuses_invalid_pages(sleep: Sleeper, body: dict[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(ReportError) as caught:
        await read_ads(handler, sleep)
    assert caught.value.reason == "unavailable"


async def test_ads_report_refuses_a_repeated_page_token(sleep: Sleeper) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [ads_row()], "nextPageToken": "loop"})

    with pytest.raises(ReportError, match="pagination"):
        await read_ads(handler, sleep)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"customer": "123"},
        {"customer": "123-456-7890"},
        {"login": "98765"},
        {"resource": "campaign; DROP"},
        {"resource": "Campaign"},
        {"fields": ["customer.id", "segments.date FROM x"]},
        {"fields": ["metrics.clicks", "campaign.id'"]},
    ],
)
async def test_ads_report_invalid_ids_and_names_are_request_failed(
    sleep: Sleeper, kwargs: dict[str, Any]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request for an invalid definition")

    with pytest.raises(ReportError) as caught:
        await read_ads(handler, sleep, **kwargs)
    assert caught.value.reason == "request_failed" and not caught.value.auth


async def test_ads_invalid_api_version_is_request_failed(sleep: Sleeper) -> None:
    client = GoogleAdsReports(
        ads_config(version="latest"),
        transport=httpx.MockTransport(with_token(lambda r: httpx.Response(200))),
        sleep=sleep,
    )
    with pytest.raises(ReportError) as caught:
        await client.list_customers(REFRESH)
    assert caught.value.reason == "request_failed"


async def test_ads_disabled_customer_report_is_customer_not_enabled(
    sleep: Sleeper, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="data-sources.google_ads.client")

    def handler(request: httpx.Request) -> httpx.Response:
        return not_enabled()

    with pytest.raises(ReportError) as caught:
        await read_ads(handler, sleep)
    assert caught.value.reason == "customer_not_enabled" and not caught.value.auth
    assert sleep.waits == []
    assert "CUSTOMER_NOT_ENABLED" in caplog.text and "req-1" in caplog.text


async def test_ads_other_400_is_request_failed(sleep: Sleeper) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": 400, "message": "bad GAQL"}})

    with pytest.raises(ReportError) as caught:
        await read_ads(handler, sleep)
    assert caught.value.reason == "request_failed" and "GAQL" not in str(caught.value)
    assert caught.value.diagnostics == "bad GAQL"


async def test_ads_refused_query_says_which_field_in_the_diagnostics_only(sleep: Sleeper) -> None:
    body = {
        "error": {
            "code": 400,
            "status": "INVALID_ARGUMENT",
            "message": "Request contains an invalid argument.",
            "details": [
                {
                    "requestId": "req-9",
                    "errors": [
                        {
                            "errorCode": {"queryError": "UNRECOGNIZED_FIELD"},
                            "message": f"Bad field 'metrics.video_views' {DEVELOPER_TOKEN}",
                        }
                    ],
                }
            ],
        }
    }
    with pytest.raises(ReportError) as caught:
        await read_ads(lambda _: httpx.Response(400, json=body), sleep)
    error = caught.value
    assert "video_views" not in str(error) and "video_views" not in error.message
    assert error.diagnostics == (
        "UNRECOGNIZED_FIELD: Bad field 'metrics.video_views' [redacted] (request req-9)"
    )


async def test_ads_refused_refresh_token_is_auth_failed_and_logged_redacted(
    sleep: Sleeper, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="data-sources.google_ads.client")

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == GOOGLE_TOKEN_URL
        return httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": f"Bad {REFRESH} for client secret {CLIENT_SECRET}",
            },
        )

    with pytest.raises(ReportAuthError) as caught:
        await ads(handler, sleep).list_customers(REFRESH)
    assert caught.value.reason == "auth_failed"
    assert REFRESH not in str(caught.value)
    assert "invalid_grant" in caplog.text and "[redacted]" in caplog.text
    assert REFRESH not in caplog.text and CLIENT_SECRET not in caplog.text


async def test_ads_code_exchange_logs_refusals(
    sleep: Sleeper, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="data-sources.google_ads.client")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "code single-use was used"}
        )

    with pytest.raises(ReportAuthError):
        await ads(handler, sleep).exchange_code("single-use")
    assert "invalid_grant" in caplog.text and "single-use" not in caplog.text


# --- Google Ads error log -------------------------------------------------------------------


def test_google_ads_error_codes() -> None:
    assert google_ads_error_codes(not_enabled()) == {"CUSTOMER_NOT_ENABLED"}
    assert google_ads_error_codes(httpx.Response(403, text="nope")) == set()
    assert google_ads_error_codes(httpx.Response(403, json={"error": "x"})) == set()
    assert google_ads_error_codes(httpx.Response(403, json=[1])) == set()


def test_log_redacts_the_bearer_and_developer_tokens(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="data-sources.google_ads.client")
    request = httpx.Request(
        "POST",
        f"https://googleads.googleapis.com/v21/customers/{ACCOUNT}/googleAds:search",
        headers={"Authorization": f"Bearer {ACCESS}", "developer-token": DEVELOPER_TOKEN},
        json={"query": "SELECT secret_query FROM campaign"},
    )
    response = httpx.Response(
        401,
        request=request,
        headers={"request-id": "header-req"},
        json={
            "error": {
                "code": 401,
                "status": "UNAUTHENTICATED",
                "message": f"Token {ACCESS} is bad;\n developer token {DEVELOPER_TOKEN}",
                "details": [
                    {
                        "requestId": "body-req",
                        "reason": f"bad {CLIENT_SECRET}",
                        "errors": [
                            {
                                "errorCode": {"authenticationError": "OAUTH_TOKEN_INVALID"},
                                "message": f"secret {CLIENT_SECRET}",
                                "trigger": {"stringValue": "should-not-be-logged"},
                            }
                        ],
                    }
                ],
            }
        },
    )
    log_google_ads_error(response, (CLIENT_SECRET,))
    records = [r for r in caplog.records if r.name == "data-sources.google_ads.client"]
    assert len(records) == 1 and records[0].levelno == logging.WARNING
    text = records[0].getMessage()
    for secret in (ACCESS, DEVELOPER_TOKEN, CLIENT_SECRET):
        assert secret not in text
    details = json.loads(text.split(": ", 1)[1])
    assert details["http_status"] == 401 and details["method"] == "POST"
    assert (
        details["endpoint"] == f"googleads.googleapis.com/v21/customers/{ACCOUNT}/googleAds:search"
    )
    assert details["status"] == "UNAUTHENTICATED"
    assert details["message"] == "Token [redacted] is bad; developer token [redacted]"
    assert details["request_id"] == "body-req"
    assert details["reasons"] == ["bad [redacted]"]
    assert details["errors"] == [
        {
            "error_code": {"authenticationError": "OAUTH_TOKEN_INVALID"},
            "message": "secret [redacted]",
        }
    ]
    assert "secret_query" not in text and "should-not-be-logged" not in text


def test_log_redacts_the_refresh_token_and_client_secret_of_a_token_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="data-sources.google_ads.client")
    refresh = "1//refresh+token/with=chars"
    request = httpx.Request(
        "POST",
        GOOGLE_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": "client-1",
            "client_secret": CLIENT_SECRET,
        },
    )
    response = httpx.Response(
        400,
        request=request,
        json={
            "error": "invalid_grant",
            "error_description": (
                f"{refresh} / {quote(refresh, safe='')} / {quote_plus(refresh)} / {CLIENT_SECRET}"
            ),
        },
    )
    log_google_ads_error(response)
    text = caplog.text
    assert "invalid_grant" in text
    for secret in (refresh, quote(refresh, safe=""), quote_plus(refresh), CLIENT_SECRET):
        assert secret not in text
    assert text.count("[redacted]") >= 4


def test_log_without_structured_details(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="data-sources.google_ads.client")
    request = httpx.Request("GET", "https://googleads.googleapis.com/v21/customers")
    log_google_ads_error(httpx.Response(502, request=request, text="<html>gateway</html>"))
    assert "Provider returned no structured error details." in caplog.text
    assert "gateway" not in caplog.text
