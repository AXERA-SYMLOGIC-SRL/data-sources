"""The Business Profile report client against `httpx.MockTransport`."""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest
from test_reports_google import REFRESH, Handler, Sleeper, config, with_token

from data_sources.reports import ReportAuthError, ReportError, create_report_source
from data_sources.reports.sources import google_business_profile as gbp
from data_sources.reports.sources.google_business_profile import GoogleBusinessProfileReports

LOCATION = "accounts/111/locations/222"
CREDS = {"refresh_token": REFRESH}


@pytest.fixture
def sleep() -> Sleeper:
    return Sleeper()


def profile(handler: Handler, sleep: Sleeper) -> GoogleBusinessProfileReports:
    source = create_report_source(
        config("google_business_profile"), transport=httpx.MockTransport(handler), sleep=sleep
    )
    assert isinstance(source, GoogleBusinessProfileReports)
    return source


def test_consent_asks_for_the_business_scope() -> None:
    url = profile(lambda r: httpx.Response(500), Sleeper()).authorize_url("s")
    assert "business.manage" in url


async def test_lists_locations_of_every_account(sleep: Sleeper) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/v1/accounts":
            if request.url.params.get("pageToken") == "next":
                return httpx.Response(200, json={"accounts": [{"name": "accounts/2"}]})
            return httpx.Response(
                200,
                json={
                    "accounts": [{"name": "accounts/1", "accountName": "Me"}],
                    "nextPageToken": "next",
                },
            )
        assert request.url.params["readMask"] == "name,title,storefrontAddress"
        if path == "/v1/accounts/1/locations":
            return httpx.Response(
                200,
                json={
                    "locations": [
                        {
                            "name": "locations/10",
                            "title": "Shop",
                            "storefrontAddress": {"locality": "Cluj", "regionCode": "RO"},
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"locations": [{"name": "locations/20"}]})

    found = await profile(with_token(handler), sleep).list_accounts(CREDS)
    assert [(a.id, a.name, a.group) for a in found] == [
        ("accounts/1/locations/10", "Shop", "Me"),
        ("accounts/2/locations/20", "locations/20", ""),
    ]
    assert found[0].details == {"locality": "Cluj", "region_code": "RO"}


@pytest.mark.parametrize(
    "accounts", [{"accounts": "x"}, {"accounts": [{"name": "nope"}]}, {"accounts": [1]}]
)
async def test_invalid_accounts_fail(sleep: Sleeper, accounts: dict[str, Any]) -> None:
    handler = with_token(lambda r: httpx.Response(200, json=accounts))
    with pytest.raises(ReportError):
        await profile(handler, sleep).list_accounts(CREDS)


def series(metric: str, points: dict[date, str | None]) -> dict[str, Any]:
    return {
        "dailyMetric": metric,
        "timeSeries": {
            "datedValues": [
                {
                    "date": {"year": d.year, "month": d.month, "day": d.day},
                    **({"value": v} if v else {}),
                }
                for d, v in points.items()
            ]
        },
    }


async def test_daily_metrics_make_a_record_per_day_with_zeros_filled(sleep: Sleeper) -> None:
    seen: list[httpx.Request] = []
    d1, d2 = date(2026, 10, 1), date(2026, 10, 2)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path == "/v1/locations/222:fetchMultiDailyMetricsTimeSeries"
        return httpx.Response(
            200,
            json={
                "multiDailyMetricTimeSeries": [
                    {
                        "dailyMetricTimeSeries": [
                            series("WEBSITE_CLICKS", {d1: "5", d2: None}),
                            series("CALL_CLICKS", {d1: "2"}),
                        ]
                    }
                ]
            },
        )

    report = await profile(with_token(handler), sleep).read(
        CREDS, LOCATION, {"report": "daily_metrics"}, d1, d2
    )
    assert report.days == [d1, d2]
    assert report.records == [
        {
            "date": {"year": 2026, "month": 10, "day": 1},
            "metrics": {"WEBSITE_CLICKS": "5", "CALL_CLICKS": "2"},
        },
        {"date": {"year": 2026, "month": 10, "day": 2}, "metrics": {"WEBSITE_CLICKS": "0"}},
    ]
    query = seen[0].url.params
    assert query.get_list("dailyMetrics") == list(gbp.DAILY_METRICS)
    assert query["dailyRange.start_date.day"] == "1" and query["dailyRange.end_date.day"] == "2"
    assert report.envelope["location"] == LOCATION


async def test_daily_metrics_window_long_ranges(sleep: Sleeper) -> None:
    ranges: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params
        ranges.append((q["dailyRange.start_date.month"], q["dailyRange.end_date.month"]))
        return httpx.Response(200, json={})

    report = await profile(with_token(handler), sleep).read_daily_metrics(
        REFRESH, LOCATION, date(2026, 9, 1), date(2026, 10, 5)
    )
    assert ranges == [("9", "10"), ("10", "10")] and report.records == []


@pytest.mark.parametrize(
    "entry",
    [
        {"dailyMetric": "CALL_CLICKS", "timeSeries": {"datedValues": [{"value": "1"}]}},
        {
            "dailyMetric": "CALL_CLICKS",
            "timeSeries": {
                "datedValues": [{"date": {"year": 2026, "month": 9, "day": 1}, "value": "1"}]
            },
        },
        {
            "dailyMetric": "CALL_CLICKS",
            "timeSeries": {
                "datedValues": [{"date": {"year": 2026, "month": 10, "day": 1}, "value": "many"}]
            },
        },
        {"dailyMetric": "NOT_A_METRIC", "timeSeries": {"datedValues": []}},
    ],
)
async def test_invalid_metric_points_fail(sleep: Sleeper, entry: dict[str, Any]) -> None:
    body = {"multiDailyMetricTimeSeries": [{"dailyMetricTimeSeries": [entry]}]}
    handler = with_token(lambda r: httpx.Response(200, json=body))
    with pytest.raises(ReportError):
        await profile(handler, sleep).read_daily_metrics(
            REFRESH, LOCATION, date(2026, 10, 1), date(2026, 10, 2)
        )


async def test_search_keywords_are_read_a_month_at_a_time(sleep: Sleeper, monkeypatch) -> None:
    monkeypatch.setattr(gbp, "_today", lambda: date(2026, 2, 10))
    months: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params
        assert request.url.path == "/v1/locations/222/searchkeywords/impressions/monthly"
        start = (q["monthlyRange.start_month.year"], q["monthlyRange.start_month.month"])
        assert start == (q["monthlyRange.end_month.year"], q["monthlyRange.end_month.month"])
        months.append(start)
        if q.get("pageToken") == "p2":
            return httpx.Response(
                200,
                json={
                    "searchKeywordsCounts": [
                        {"searchKeyword": "b", "insightsValue": {"threshold": "15"}}
                    ]
                },
            )
        hits = [{"searchKeyword": "a", "insightsValue": {"value": "40"}}]
        extra = {"nextPageToken": "p2"} if start == ("2026", "1") else {}
        return httpx.Response(200, json={"searchKeywordsCounts": hits, **extra})

    report = await profile(with_token(handler), sleep).read(
        CREDS,
        LOCATION,
        {"report": "search_keywords", "months": 3},
        date(2026, 1, 1),
        date(2026, 1, 2),
    )
    # The third month has a second page.
    assert months == [("2025", "11"), ("2025", "12"), ("2026", "1"), ("2026", "1")]
    assert report.days == [None] * 4
    assert report.records[2] == {
        "month": {"year": 2026, "month": 1},
        "searchKeyword": "a",
        "insightsValue": {"value": "40"},
    }
    assert report.records[3]["insightsValue"] == {"threshold": "15"}
    assert report.envelope["months"] == ["2025-11", "2025-12", "2026-01"]


async def test_reviews_are_read_whole_with_the_summary(sleep: Sleeper) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v4/{LOCATION}/reviews"
        assert request.url.params["pageSize"] == "50"
        if request.url.params.get("pageToken") == "n":
            return httpx.Response(200, json={"reviews": [{"reviewId": "b", "starRating": "ONE"}]})
        return httpx.Response(
            200,
            json={
                "reviews": [{"reviewId": "a", "starRating": "FIVE"}],
                "averageRating": 4.5,
                "totalReviewCount": 2,
                "nextPageToken": "n",
            },
        )

    report = await profile(with_token(handler), sleep).read(
        CREDS, LOCATION, {"report": "reviews"}, date(2026, 10, 1), date(2026, 10, 1)
    )
    assert [r["reviewId"] for r in report.records] == ["a", "b"] and report.days == [None, None]
    assert report.envelope == {"location": LOCATION, "averageRating": 4.5, "totalReviewCount": 2}


@pytest.mark.parametrize("location", ["locations/222", "accounts/1", "accounts/1/locations/x", ""])
async def test_refuses_an_invalid_location_before_any_request(
    sleep: Sleeper, location: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request")

    for report in ("daily_metrics", "search_keywords", "reviews"):
        with pytest.raises(ReportError) as error:
            await profile(handler, sleep).read(
                CREDS, location, {"report": report}, date(2026, 10, 1), date(2026, 10, 1)
            )
        assert error.value.reason == "request_failed"


async def test_refuses_an_unknown_report(sleep: Sleeper) -> None:
    with pytest.raises(ReportError) as error:
        await profile(lambda r: httpx.Response(500), sleep).read(
            CREDS, LOCATION, {"report": "photos"}, date(2026, 10, 1), date(2026, 10, 1)
        )
    assert error.value.reason == "request_failed"


@pytest.mark.parametrize("status", [401, 403])
async def test_access_errors_are_auth_failures(sleep: Sleeper, status: int) -> None:
    handler = with_token(
        lambda r: httpx.Response(status, json={"error": {"message": "secret detail"}})
    )
    with pytest.raises(ReportAuthError) as error:
        await profile(handler, sleep).read_reviews(REFRESH, LOCATION)
    assert "secret detail" not in str(error.value)


async def test_zero_quota_is_reported_as_unavailable_after_retries(sleep: Sleeper) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429, json={"error": {"status": "RESOURCE_EXHAUSTED", "message": "limit 0"}}
        )

    with pytest.raises(ReportError) as error:
        await profile(with_token(handler), sleep).list_accounts(CREDS)
    assert error.value.reason == "unavailable" and calls == 3 and "approve" in str(error.value)
