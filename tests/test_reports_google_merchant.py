"""The Merchant Center report client against `httpx.MockTransport`."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx
import pytest
from test_reports_google import (
    REFRESH,
    Handler,
    Sleeper,
    body_of,
    config,
    token_refused,
    with_token,
)

from data_sources.reports import ReportAuthError, ReportError, create_report_source
from data_sources.reports.sources.google_merchant import GoogleMerchantReports

ACCOUNT = "123456789"
FIELDS = ["date", "offer_id", "marketing_method", "clicks", "impressions"]


@pytest.fixture
def sleep() -> Sleeper:
    return Sleeper()


def merchant(handler: Handler, sleep: Sleeper) -> GoogleMerchantReports:
    source = create_report_source(
        config("google_merchant"), transport=httpx.MockTransport(handler), sleep=sleep
    )
    assert isinstance(source, GoogleMerchantReports)
    return source


def row(day: date, offer: str = "sku-1") -> dict[str, Any]:
    return {
        "productPerformanceView": {
            "date": {"year": day.year, "month": day.month, "day": day.day},
            "offerId": offer,
            "marketingMethod": "ORGANIC",
            "clicks": "12",
            "impressions": "340",
        }
    }


def test_consent_asks_for_the_content_scope() -> None:
    url = merchant(lambda r: httpx.Response(500), Sleeper()).authorize_url("state-1")
    assert "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcontent+openid+email" in url


async def test_lists_accounts_over_pages(sleep: Sleeper) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/accounts/v1/accounts"
        if request.url.params.get("pageToken") == "next":
            return httpx.Response(200, json={"accounts": [{"accountId": "2", "accountName": ""}]})
        return httpx.Response(
            200,
            json={
                "accounts": [
                    {
                        "name": "accounts/1",
                        "accountId": "1",
                        "accountName": "Shop",
                        "timeZone": {"id": "Europe/Bucharest"},
                        "languageCode": "ro",
                    }
                ],
                "nextPageToken": "next",
            },
        )

    accounts = await merchant(with_token(handler), sleep).list_accounts({"refresh_token": REFRESH})
    assert [(a.id, a.name) for a in accounts] == [("1", "Shop"), ("2", "2")]
    assert accounts[0].details == {
        "time_zone": "Europe/Bucharest",
        "language_code": "ro",
        "test_account": False,
    }


async def test_reads_windows_and_pages(sleep: Sleeper) -> None:
    seen: list[dict[str, Any]] = []
    first, last = date(2026, 9, 1), date(2026, 10, 5)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/reports/v1/accounts/{ACCOUNT}/reports:search"
        body = body_of(request)
        seen.append(body)
        day = date.fromisoformat(body["query"].split("BETWEEN '")[1][:10])
        if "pageToken" not in body and day == first:
            return httpx.Response(200, json={"results": [row(day)], "nextPageToken": "p2"})
        return httpx.Response(200, json={"results": [row(day, "sku-2")]})

    report = await merchant(with_token(handler), sleep).read(
        {"refresh_token": REFRESH},
        ACCOUNT,
        {"view": "product_performance_view", "fields": FIELDS},
        first,
        last,
    )
    assert [b["query"] for b in seen if "pageToken" not in b] == [
        "SELECT date, offer_id, marketing_method, clicks, impressions "
        "FROM product_performance_view "
        "WHERE date BETWEEN '2026-09-01' AND '2026-10-01'",
        "SELECT date, offer_id, marketing_method, clicks, impressions "
        "FROM product_performance_view "
        "WHERE date BETWEEN '2026-10-02' AND '2026-10-05'",
    ]
    assert seen[1]["pageToken"] == "p2"
    assert [r["productPerformanceView"]["offerId"] for r in report.records] == [
        "sku-1",
        "sku-2",
        "sku-2",
    ]
    assert report.days == [first, first, date(2026, 10, 2)]
    assert report.envelope == {
        "accountId": ACCOUNT,
        "view": "product_performance_view",
        "fields": FIELDS,
    }


@pytest.mark.parametrize(
    "view, fields, account",
    [
        ("product_view", FIELDS, ACCOUNT),
        ("product_performance_view", ["offer_id", "clicks"], ACCOUNT),
        ("product_performance_view", [*FIELDS[:-1], "clicks; DROP"], ACCOUNT),
        ("product_performance_view", FIELDS, "accounts/1"),
    ],
)
async def test_refuses_an_invalid_definition_before_any_request(
    sleep: Sleeper, view: str, fields: list[str], account: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request for an invalid definition")

    with pytest.raises(ReportError) as error:
        await merchant(handler, sleep).read_report(
            REFRESH, account, view, fields, date(2026, 10, 1), date(2026, 10, 2)
        )
    assert error.value.reason == "request_failed"


@pytest.mark.parametrize(
    "bad",
    [
        {"productPerformanceView": {"date": {"year": 2026, "month": 10, "day": 9}, "clicks": "1"}},
        {
            "productPerformanceView": {
                "date": {"year": 2026, "month": 10, "day": 1},
                "clicks": "many",
            }
        },
        {"productPerformanceView": {"clicks": "1"}},
        {"other": {}},
    ],
)
async def test_an_invalid_row_fails_the_read(sleep: Sleeper, bad: dict[str, Any]) -> None:
    handler = with_token(lambda request: httpx.Response(200, json={"results": [bad]}))
    with pytest.raises(ReportError) as error:
        await merchant(handler, sleep).read_report(
            REFRESH,
            ACCOUNT,
            "product_performance_view",
            FIELDS,
            date(2026, 10, 1),
            date(2026, 10, 2),
        )
    assert error.value.reason == "unavailable"


async def test_retries_rate_limits_then_succeeds(sleep: Sleeper) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}})
        return httpx.Response(200, json={})

    report = await merchant(with_token(handler), sleep).read_report(
        REFRESH, ACCOUNT, "product_performance_view", FIELDS, date(2026, 10, 1), date(2026, 10, 1)
    )
    assert report.records == [] and calls == 2 and sleep.waits == [1.0]


@pytest.mark.parametrize("status", [401, 403])
async def test_access_errors_are_auth_failures_without_the_response_body(
    sleep: Sleeper, status: int
) -> None:
    handler = with_token(
        lambda request: httpx.Response(status, json={"error": {"message": "secret detail"}})
    )
    with pytest.raises(ReportAuthError) as error:
        await merchant(handler, sleep).list_accounts({"refresh_token": REFRESH})
    assert "secret detail" not in str(error.value)


async def test_a_revoked_refresh_token_is_an_auth_failure(sleep: Sleeper) -> None:
    with pytest.raises(ReportAuthError):
        await merchant(token_refused, sleep).list_accounts({"refresh_token": REFRESH})


async def test_other_refusals_are_request_failures(sleep: Sleeper) -> None:
    handler = with_token(lambda request: httpx.Response(400, json=json.loads('{"error": {}}')))
    with pytest.raises(ReportError) as error:
        await merchant(handler, sleep).read_report(
            REFRESH,
            ACCOUNT,
            "product_performance_view",
            FIELDS,
            date(2026, 10, 1),
            date(2026, 10, 1),
        )
    assert error.value.reason == "request_failed"
