"""The Merchant Center report client against `httpx.MockTransport`."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
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
        "mode": "daily",
        "fields": FIELDS,
        "filters": {},
    }


@pytest.mark.parametrize(
    "view, fields, account",
    [
        ("bogus_view", FIELDS, ACCOUNT),
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


def snapshot_row(offer: str, **extra: Any) -> dict[str, Any]:
    return {"productView": {"id": f"online~en~US~{offer}", "offerId": offer, **extra}}


async def test_a_snapshot_is_read_as_it_is_now_and_labelled_with_today(sleep: Sleeper) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(body_of(request))
        return httpx.Response(200, json={"results": [snapshot_row("a"), snapshot_row("b")]})

    report = await merchant(with_token(handler), sleep).read(
        {"refresh_token": REFRESH},
        ACCOUNT,
        {"view": "product_view", "fields": ["id", "offer_id"], "mode": "snapshot"},
        date(2026, 10, 1),
        date(2026, 10, 2),
    )
    assert [b["query"] for b in seen] == ["SELECT id, offer_id FROM product_view"]
    assert report.days == [datetime.now(UTC).date()] * 2 and len(report.records) == 2


async def test_a_trailing_window_ends_on_the_last_day(sleep: Sleeper) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(body_of(request))
        return httpx.Response(200, json={})

    await merchant(with_token(handler), sleep).read(
        {"refresh_token": REFRESH},
        ACCOUNT,
        {
            "view": "competitive_visibility_top_merchant_view",
            "fields": ["domain", "rank"],
            "mode": "snapshot",
            "trailing_days": 7,
        },
        date(2026, 10, 1),
        date(2026, 10, 20),
    )
    assert seen[0]["query"] == (
        "SELECT domain, rank FROM competitive_visibility_top_merchant_view "
        "WHERE date BETWEEN '2026-10-14' AND '2026-10-20'"
    )


async def test_filters_make_one_query_per_combination(sleep: Sleeper) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(body_of(request)["query"])
        return httpx.Response(200, json={})

    report = await merchant(with_token(handler), sleep).read_report(
        REFRESH,
        ACCOUNT,
        "best_sellers_product_cluster_view",
        ["report_date", "rank"],
        date(2026, 10, 1),
        date(2026, 10, 1),
        mode="snapshot",
        filters={"report_granularity": ["WEEKLY"], "report_country_code": ["'RO'", "'DE'"]},
    )
    assert seen == [
        "SELECT report_date, rank FROM best_sellers_product_cluster_view "
        "WHERE report_granularity = WEEKLY AND report_country_code = 'RO'",
        "SELECT report_date, rank FROM best_sellers_product_cluster_view "
        "WHERE report_granularity = WEEKLY AND report_country_code = 'DE'",
    ]
    assert report.envelope["filters"] == {
        "report_granularity": ["WEEKLY"],
        "report_country_code": ["'RO'", "'DE'"],
    }


async def test_a_filter_without_values_reads_nothing(sleep: Sleeper) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request, not even for a token")

    report = await merchant(handler, sleep).read_report(
        REFRESH,
        ACCOUNT,
        "best_sellers_brand_view",
        ["rank"],
        date(2026, 10, 1),
        date(2026, 10, 1),
        mode="snapshot",
        filters={"report_country_code": []},
    )
    assert report.records == [] and report.days == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "snapshot", "fields": ["date", "clicks"]},
        {"mode": "daily", "fields": ["clicks"]},
        {"mode": "weekly", "fields": ["clicks"]},
        {"mode": "daily", "fields": ["date", "clicks"], "trailing_days": 3},
        {"mode": "snapshot", "fields": ["rank"], "filters": {"c": ["'RO' OR 1=1"]}},
        {"mode": "snapshot", "fields": ["rank"], "filters": {"country; DROP": ["'RO'"]}},
    ],
)
async def test_refuses_an_inconsistent_mode_or_filter(
    sleep: Sleeper, kwargs: dict[str, Any]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request for an invalid definition")

    fields = kwargs.pop("fields")
    with pytest.raises(ReportError) as error:
        await merchant(handler, sleep).read_report(
            REFRESH, ACCOUNT, "product_view", fields, date(2026, 10, 1), date(2026, 10, 1), **kwargs
        )
    assert error.value.reason == "request_failed"
