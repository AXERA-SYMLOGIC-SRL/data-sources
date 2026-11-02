"""Bing Webmaster Tools against `httpx.MockTransport`."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from data_sources.config.schema import AuthConfig, ConnectorConfig
from data_sources.reports import ReportAuthError, ReportError, create_report_source
from data_sources.reports.errors import Problem
from data_sources.reports.http import Retry
from data_sources.reports.sources import bing_webmaster as bing
from data_sources.reports.sources.bing_webmaster import (
    BingWebmasterReports,
    classify,
    parse_wcf_date,
)

CLIENT_ID = "client-id"
SECRET = "client-secret"
REDIRECT = "http://clearpond.test:5173/api/v1/oauth/bing-webmaster/callback"
SITE = "https://example.com/"
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
CREDENTIALS = {
    "access_token": "access-1",
    "refresh_token": "refresh-1",
    "consent_refresh_token": "refresh-0",
    "expires_at": NOW.isoformat(),
}
SECRETS = ("access-1", "access-2", "refresh-0", "refresh-1", "refresh-2", SECRET, "the-code")

Handler = Callable[[httpx.Request], httpx.Response]


async def no_wait(_: float) -> None:
    pass


def reports(handler: Handler) -> BingWebmasterReports:
    config = ConnectorConfig(
        provider="bing_webmaster",
        auth=AuthConfig(
            type="oauth2",
            credentials={"client_id": CLIENT_ID, "client_secret": SECRET, "redirect_uri": REDIRECT},
        ),
    )
    source = create_report_source(config, transport=httpx.MockTransport(handler), sleep=no_wait)
    assert isinstance(source, BingWebmasterReports)
    return source


def wcf(day: date) -> str:
    """Bing's date for `day`: midnight Pacific (07:00 UTC) with a -0700 offset."""
    instant = datetime(day.year, day.month, day.day, 7, tzinfo=UTC)
    return f"/Date({int(instant.timestamp() * 1000)}-0700)/"


def query_row(day: date, query: str = "clearpond", clicks: int = 3) -> dict[str, Any]:
    return {
        "__type": "QueryStats:#Microsoft.Bing.Webmaster.Api",
        "AvgClickPosition": 2.5,
        "AvgImpressionPosition": 4.1,
        "Clicks": clicks,
        "Date": wcf(day),
        "Impressions": 40,
        "Query": query,
    }


def form(request: httpx.Request) -> dict[str, str]:
    return dict(parse_qsl(request.content.decode()))


def no_secret_in(error: BaseException) -> None:
    text = f"{error} {getattr(error, 'message', '')} {getattr(error, 'diagnostics', '')}"
    assert not any(secret in text for secret in SECRETS)


@pytest.fixture(autouse=True)
def now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bing, "_now", lambda: NOW)


def test_authorize_url_asks_for_read_access_with_the_state() -> None:
    url = urlsplit(reports(lambda _: pytest.fail("no request")).authorize_url("state-1"))
    assert (url.netloc, url.path) == ("www.bing.com", "/webmasters/oauth/authorize")
    assert dict(parse_qsl(url.query)) == {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT,
        "scope": "webmaster.read",
        "state": "state-1",
    }
    assert SECRET not in url.query


def test_configured_needs_the_client_id_and_secret() -> None:
    config = ConnectorConfig(
        provider="bing_webmaster",
        auth=AuthConfig(type="oauth2", credentials={"client_id": CLIENT_ID}),
    )
    assert not create_report_source(config).configured


async def test_exchange_code_keeps_the_consent_refresh_token_apart(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = {
            "access_token": "access-2",
            "token_type": "bearer",
            "expires_in": 3599,
            "refresh_token": "refresh-2",
        }
        return httpx.Response(200, json=body)

    with caplog.at_level(logging.INFO):
        grant = await reports(handler).exchange_code("the-code")
    (request,) = seen
    assert request.method == "POST" and str(request.url) == bing.TOKEN_URL
    assert SECRET not in str(request.url) and "the-code" not in str(request.url)
    assert form(request) == {
        "client_id": CLIENT_ID,
        "client_secret": SECRET,
        "grant_type": "authorization_code",
        "code": "the-code",
        "redirect_uri": REDIRECT,
    }
    assert grant.credentials == {
        "access_token": "access-2",
        "refresh_token": "refresh-2",
        "consent_refresh_token": "refresh-2",
        "expires_at": "2026-10-29T12:59:59+00:00",
    }
    assert grant.identity is None
    assert "refresh_token" in caplog.text  # the key names, for the live check
    assert not any(secret in caplog.text for secret in SECRETS)


async def test_no_refresh_token_in_the_exchange() -> None:
    handler = lambda _: httpx.Response(200, json={"access_token": "access-2"})  # noqa: E731
    with pytest.raises(ReportError) as caught:
        await reports(handler).exchange_code("the-code")
    assert caught.value.reason == "no_refresh_token"


@pytest.mark.parametrize("status", [400, 401])
async def test_a_refused_code_is_auth_failed_with_the_reason_apart(status: int) -> None:
    body = {"error": "invalid_grant", "error_description": "The code has expired."}
    with pytest.raises(ReportAuthError) as caught:
        await reports(lambda _: httpx.Response(status, json=body)).exchange_code("the-code")
    assert caught.value.reason == "auth_failed"
    assert "expired" not in caught.value.message and "expired" in caught.value.diagnostics
    no_secret_in(caught.value)


@pytest.mark.parametrize("status", [429, 503])
async def test_a_busy_token_endpoint_is_unavailable(status: int) -> None:
    with pytest.raises(ReportError) as caught:
        await reports(lambda _: httpx.Response(status)).exchange_code("the-code")
    assert caught.value.reason == "unavailable" and not caught.value.auth


async def test_refresh_keeps_a_rotated_refresh_token_and_the_consent_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert form(request)["grant_type"] == "refresh_token"
        assert form(request)["refresh_token"] == "refresh-1"
        body = {"access_token": "access-2", "expires_in": 3599, "refresh_token": "refresh-2"}
        return httpx.Response(200, json=body)

    with caplog.at_level(logging.INFO):
        grant = await reports(handler).refresh_grant(CREDENTIALS)
    assert grant.credentials == {
        "access_token": "access-2",
        "refresh_token": "refresh-2",
        "consent_refresh_token": "refresh-0",
        "expires_at": "2026-10-29T12:59:59+00:00",
    }
    assert "rotated=yes used=latest" in caplog.text
    assert not any(secret in caplog.text for secret in SECRETS)


async def test_refresh_falls_back_to_the_consent_token_when_the_latest_is_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(form(request)["refresh_token"])
        if form(request)["refresh_token"] == "refresh-1":
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(200, json={"access_token": "access-2", "expires_in": 3599})

    with caplog.at_level(logging.INFO):
        grant = await reports(handler).refresh_grant(CREDENTIALS)
    assert tried == ["refresh-1", "refresh-0"]
    assert grant.credentials["access_token"] == "access-2"
    assert grant.credentials["refresh_token"] == "refresh-0"  # the one that worked
    assert grant.credentials["consent_refresh_token"] == "refresh-0"
    assert "rotated=no used=consent" in caplog.text


async def test_refresh_refused_for_both_tokens_is_auth_failed() -> None:
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(form(request)["refresh_token"])
        body = {"error": "invalid_grant", "error_description": "Refresh token does not match"}
        return httpx.Response(400, json=body)

    with pytest.raises(ReportAuthError) as caught:
        await reports(handler).refresh_grant(CREDENTIALS)
    assert tried == ["refresh-1", "refresh-0"]
    assert caught.value.reason == "auth_failed" and "does not match" in caught.value.diagnostics
    no_secret_in(caught.value)


async def test_refresh_tries_a_token_once_when_both_are_the_same() -> None:
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(form(request)["refresh_token"])
        return httpx.Response(400, json={"error": "invalid_grant"})

    same = {**CREDENTIALS, "consent_refresh_token": "refresh-1"}
    with pytest.raises(ReportAuthError):
        await reports(handler).refresh_grant(same)
    assert tried == ["refresh-1"]
    with pytest.raises(ReportAuthError):
        await reports(handler).refresh_grant({"access_token": "access-1"})


async def test_an_unavailable_refresh_is_not_retried_with_the_consent_token() -> None:
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(form(request)["refresh_token"])
        return httpx.Response(503)

    with pytest.raises(ReportError) as caught:
        await reports(handler).refresh_grant(CREDENTIALS)
    assert caught.value.reason == "unavailable" and tried == ["refresh-1"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/Date(1316156400000-0700)/", date(2011, 9, 16)),
        ("/Date(1399100400000)/", date(2014, 5, 3)),
        ("/Date(1399100400000+0200)/", date(2014, 5, 3)),
        ("/Date(1316138400000+0700)/", date(2011, 9, 16)),
        ("2026-01-01", None),
        (None, None),
        ("/Date(x)/", None),
        ("/Date(1399100400000-07)/", None),
        (1399100400000, None),
    ],
)
def test_parse_wcf_date(value: object, expected: date | None) -> None:
    assert parse_wcf_date(value) == expected


async def test_sites_are_the_verified_ones_read_with_a_bearer_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "ssl.bing.com"
        assert request.url.path == "/webmaster/api.svc/json/GetUserSites"
        assert request.headers["Authorization"] == "Bearer access-1"
        assert "access-1" not in str(request.url)
        sites = [
            {"Url": SITE, "IsVerified": True, "AuthenticationCode": "abc"},
            {"Url": "https://pending.example/", "IsVerified": False},
        ]
        return httpx.Response(200, json={"d": sites})

    (account,) = await reports(handler).list_accounts(CREDENTIALS)
    assert (account.id, account.name, account.details) == (SITE, SITE, {"verified": True})


@pytest.mark.parametrize("body", [{}, {"d": None}, {"d": [{"IsVerified": True}]}, {"d": ["x"]}])
async def test_malformed_sites_are_invalid(body: dict[str, Any]) -> None:
    with pytest.raises(ReportError) as caught:
        await reports(lambda _: httpx.Response(200, json=body)).list_accounts(CREDENTIALS)
    assert caught.value.reason == "unavailable"


async def test_no_access_token_is_auth_failed_without_a_request() -> None:
    source = reports(lambda _: pytest.fail("no request"))
    with pytest.raises(ReportAuthError):
        await source.list_accounts({"refresh_token": "refresh-1"})


async def test_read_keeps_the_rows_in_the_window_and_says_what_bing_has() -> None:
    first = date(2026, 4, 25)
    rows = [query_row(date.fromordinal(first.toordinal() + 7 * n)) for n in range(26)]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"d": rows})

    since, until = date(2026, 9, 29), date(2026, 10, 28)
    report = await reports(handler).read(
        CREDENTIALS, SITE, {"report": "queries", "day_column": "Date"}, since, until
    )
    (request,) = seen
    assert request.method == "GET" and request.url.path.endswith("/GetQueryStats")
    assert request.url.params["siteUrl"] == SITE
    assert request.headers["Authorization"] == "Bearer access-1"
    kept = [r for r in rows if since <= parse_wcf_date(r["Date"]) <= until]  # type: ignore[operator]
    assert len(kept) == 3
    assert report.records == kept  # untouched
    assert report.days == [parse_wcf_date(r["Date"]) for r in kept]
    assert report.envelope == {
        "siteUrl": SITE,
        "report": "queries",
        "method": "GetQueryStats",
        "available": {"rows": 26, "first": "2026-04-25", "last": "2026-10-17"},
    }


@pytest.mark.parametrize(
    ("report", "method"),
    [
        ("traffic", "GetRankAndTrafficStats"),
        ("crawl", "GetCrawlStats"),
        ("pages", "GetPageStats"),
    ],
)
async def test_each_report_calls_its_method(report: str, method: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/{method}")
        return httpx.Response(200, json={"d": []})

    found = await reports(handler).read(
        CREDENTIALS, SITE, {"report": report}, date(2026, 10, 1), date(2026, 10, 28)
    )
    assert found.records == [] and found.days == []
    assert found.envelope["available"] == {"rows": 0, "first": None, "last": None}


@pytest.mark.parametrize(
    "row",
    [
        "not a row",
        {**query_row(date(2026, 10, 3)), "Date": "2026-10-03"},
        {**query_row(date(2026, 10, 3)), "Date": None},
        {**query_row(date(2026, 10, 3)), "Clicks": "3"},
        {**query_row(date(2026, 10, 3)), "Clicks": True},
        {**query_row(date(2026, 10, 3)), "AvgClickPosition": float("nan")},
        {**query_row(date(2026, 10, 3)), "Query": ""},
        {k: v for k, v in query_row(date(2026, 10, 3)).items() if k != "Query"},
    ],
)
async def test_malformed_rows_are_invalid(row: object) -> None:
    body = json.dumps({"d": [row]})  # allows NaN, which Python's JSON reader takes too
    with pytest.raises(ReportError) as caught:
        await reports(lambda _: httpx.Response(200, content=body)).read(
            CREDENTIALS, SITE, {"report": "queries"}, date(2026, 10, 1), date(2026, 10, 28)
        )
    assert caught.value.reason == "unavailable"


async def test_a_body_without_d_is_invalid() -> None:
    with pytest.raises(ReportError) as caught:
        await reports(lambda _: httpx.Response(200, json={"rows": []})).read(
            CREDENTIALS, SITE, {"report": "traffic"}, date(2026, 10, 1), date(2026, 10, 28)
        )
    assert caught.value.reason == "unavailable"


@pytest.mark.parametrize(
    ("spec", "since"),
    [({"report": "links"}, date(2026, 10, 1)), ({"report": "traffic"}, date(2026, 10, 29))],
)
async def test_an_unknown_report_or_reversed_window_is_refused(
    spec: dict[str, Any], since: date
) -> None:
    source = reports(lambda _: pytest.fail("no request"))
    with pytest.raises(ReportError) as caught:
        await source.read(CREDENTIALS, SITE, spec, since, date(2026, 10, 28))
    assert caught.value.reason == "request_failed"


def fault(status: int, code: int, message: str = "no") -> httpx.Response:
    request = httpx.Request("GET", f"{bing.API_URL}GetQueryStats")
    body = {"ErrorCode": code, "Message": message}
    return httpx.Response(status, json=body, request=request)


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (fault(400, 3), "auth_failed"),
        (fault(400, 14), "auth_failed"),
        (fault(400, 6), "auth_failed"),
        (httpx.Response(401), "auth_failed"),
        (httpx.Response(403), "auth_failed"),
        (fault(400, 8, "Invalid siteUrl"), "request_failed"),
        (httpx.Response(404, text="<html>no</html>"), "request_failed"),
    ],
)
def test_classify(response: httpx.Response, reason: str) -> None:
    verdict = classify(response)
    assert isinstance(verdict, Problem) and verdict.reason == reason


@pytest.mark.parametrize(
    "response",
    [
        fault(400, 4),
        fault(400, 5),
        fault(500, 1),
        httpx.Response(429),
        httpx.Response(500, text="<html>down</html>"),
    ],
)
def test_classify_retries_what_is_transient(response: httpx.Response) -> None:
    assert isinstance(classify(response), Retry)


def test_classify_accepts_a_good_answer_and_reads_a_wrapped_fault() -> None:
    assert classify(httpx.Response(200, json={"d": []})) is None
    wrapped = httpx.Response(200, json={"d": {"ErrorCode": 14, "Message": "no"}})
    assert classify(wrapped) == bing.AUTH_FAILED


async def test_a_refused_request_keeps_bings_reason_on_the_diagnostics() -> None:
    with pytest.raises(ReportError) as caught:
        await reports(lambda _: fault(400, 8, "Invalid   siteUrl")).read(
            CREDENTIALS, SITE, {"report": "queries"}, date(2026, 10, 1), date(2026, 10, 28)
        )
    assert caught.value.reason == "request_failed"
    assert caught.value.diagnostics == "8: Invalid siteUrl"
    no_secret_in(caught.value)


async def test_throttling_is_retried_then_unavailable() -> None:
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return fault(400, 4)

    with pytest.raises(ReportError) as caught:
        await reports(handler).read(
            CREDENTIALS, SITE, {"report": "traffic"}, date(2026, 10, 1), date(2026, 10, 28)
        )
    assert caught.value.reason == "unavailable" and len(calls) == 3


async def test_an_auth_fault_is_an_auth_error_without_the_token() -> None:
    with pytest.raises(ReportAuthError) as caught:
        await reports(lambda _: fault(400, 14, "Not authorized")).list_accounts(CREDENTIALS)
    no_secret_in(caught.value)
