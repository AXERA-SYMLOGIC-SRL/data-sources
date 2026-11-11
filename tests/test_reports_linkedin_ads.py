"""LinkedIn Ads against `httpx.MockTransport`."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from data_sources.config.schema import AuthConfig, ConnectorConfig
from data_sources.reports import ReportAuthError, ReportError, create_report_source
from data_sources.reports.errors import Problem
from data_sources.reports.http import Retry
from data_sources.reports.sources import linkedin_ads as li
from data_sources.reports.sources.linkedin_ads import LinkedInAdsReports, classify

CLIENT_ID = "client-id"
SECRET = "client-secret"
REDIRECT = "http://localhost:5173/api/v1/oauth/linkedin/callback"
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
CREDENTIALS = {
    "access_token": "access-1",
    "expires_at": NOW.isoformat(),
    "refresh_token": "refresh-1",
    "refresh_expires_at": "2027-11-11T12:00:00+00:00",
    "scope": "r_ads,r_ads_reporting",
}
SECRETS = ("access-1", "access-2", "refresh-1", "refresh-2", SECRET, "the-code")
ACCOUNT = "508123456"

Handler = Callable[[httpx.Request], httpx.Response]


async def no_wait(_: float) -> None:
    pass


def reports(handler: Handler, **options: str) -> LinkedInAdsReports:
    config = ConnectorConfig(
        provider="linkedin_ads",
        auth=AuthConfig(
            type="oauth2",
            credentials={"client_id": CLIENT_ID, "client_secret": SECRET, "redirect_uri": REDIRECT},
        ),
        options=options,
    )
    source = create_report_source(config, transport=httpx.MockTransport(handler), sleep=no_wait)
    assert isinstance(source, LinkedInAdsReports)
    return source


def no_secret_in(error: BaseException) -> None:
    text = f"{error} {getattr(error, 'message', '')} {getattr(error, 'diagnostics', '')}"
    assert not any(secret in text for secret in SECRETS)


def day_of(day: date) -> dict[str, Any]:
    return {"year": day.year, "month": day.month, "day": day.day}


def element(urn: str, day: date, **metrics: Any) -> dict[str, Any]:
    return {
        "dateRange": {"start": day_of(day), "end": day_of(day)},
        "pivotValues": [urn],
        **metrics,
    }


def query(request: httpx.Request) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(str(request.url)).query))


@pytest.fixture(autouse=True)
def now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(li, "_now", lambda: NOW)


def test_authorize_url_asks_for_both_scopes_with_the_state() -> None:
    url = urlsplit(reports(lambda _: pytest.fail("no request")).authorize_url("state-1"))
    assert (url.netloc, url.path) == ("www.linkedin.com", "/oauth/v2/authorization")
    assert dict(parse_qsl(url.query)) == {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT,
        "scope": "r_ads r_ads_reporting",
        "state": "state-1",
    }
    assert SECRET not in url.query


async def test_exchange_code_with_a_refresh_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = {
            "access_token": "access-2",
            "expires_in": 5183999,
            "refresh_token": "refresh-2",
            "refresh_token_expires_in": 31536000,
            "scope": "r_ads,r_ads_reporting",
        }
        return httpx.Response(200, json=body)

    grant = await reports(handler).exchange_code("the-code")
    (request,) = seen
    assert request.method == "POST" and str(request.url) == li.TOKEN_URL
    assert "the-code" not in str(request.url)
    assert dict(parse_qsl(request.content.decode()))["grant_type"] == "authorization_code"
    assert grant.credentials == {
        "access_token": "access-2",
        "expires_at": (NOW + timedelta(seconds=5183999)).isoformat(),
        "refresh_token": "refresh-2",
        "refresh_expires_at": (NOW + timedelta(seconds=31536000)).isoformat(),
        "scope": "r_ads,r_ads_reporting",
    }
    assert grant.expires_in == 31536000


async def test_exchange_code_without_a_refresh_token_expires_with_the_access_token() -> None:
    body = {"access_token": "access-2", "expires_in": 5183999, "scope": "r_ads,r_ads_reporting"}
    grant = await reports(lambda _: httpx.Response(200, json=body)).exchange_code("the-code")
    assert "refresh_token" not in grant.credentials
    assert grant.expires_in == 5183999


async def test_a_consent_without_reporting_access_is_denied() -> None:
    body = {"access_token": "access-2", "expires_in": 100, "scope": "r_ads"}
    with pytest.raises(ReportError) as caught:
        await reports(lambda _: httpx.Response(200, json=body)).exchange_code("the-code")
    assert caught.value.reason == "access_denied"
    no_secret_in(caught.value)


async def test_the_exchange_is_not_retried() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    with pytest.raises(ReportError) as caught:
        await reports(handler).exchange_code("the-code")
    assert caught.value.reason == "unavailable" and calls == 1


@pytest.mark.parametrize("status", [400, 401])
async def test_a_refused_code_is_auth_failed_with_the_reason_apart(status: int) -> None:
    body = {"error": "invalid_request", "error_description": "The code has expired."}
    with pytest.raises(ReportAuthError) as caught:
        await reports(lambda _: httpx.Response(status, json=body)).exchange_code("the-code")
    assert "expired" not in caught.value.message and "expired" in caught.value.diagnostics
    no_secret_in(caught.value)


async def test_refresh_renews_and_keeps_what_the_answer_leaves_out() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        sent = dict(parse_qsl(request.content.decode()))
        assert sent["grant_type"] == "refresh_token" and sent["refresh_token"] == "refresh-1"
        return httpx.Response(200, json={"access_token": "access-2", "expires_in": 3600})

    grant = await reports(handler).refresh_grant(CREDENTIALS)
    assert grant.credentials == {
        "access_token": "access-2",
        "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        "refresh_token": "refresh-1",
        "refresh_expires_at": "2027-11-11T12:00:00+00:00",
        "scope": "r_ads,r_ads_reporting",
    }


async def test_refresh_without_a_refresh_token_is_auth_failed() -> None:
    credentials = {"access_token": "access-1", "expires_at": NOW.isoformat()}
    with pytest.raises(ReportAuthError):
        await reports(lambda _: pytest.fail("no request")).refresh_grant(credentials)


async def test_a_refused_refresh_token_is_auth_failed() -> None:
    body = {"error": "invalid_grant", "error_description": "expired refresh token"}
    with pytest.raises(ReportAuthError) as caught:
        await reports(lambda _: httpx.Response(400, json=body)).refresh_grant(CREDENTIALS)
    no_secret_in(caught.value)


async def test_list_accounts_follows_the_pages() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer access-1"
        if "pageToken" not in query(request):
            body = {
                "elements": [
                    {"id": 508123456, "name": "Acme", "currency": "EUR", "status": "ACTIVE"}
                ],
                "metadata": {"nextPageToken": "tok/en=="},
            }
        else:
            assert query(request)["pageToken"] == "tok/en=="
            body = {"elements": [{"id": 7, "currency": "USD", "type": "BUSINESS"}]}
        return httpx.Response(200, json=body)

    accounts = await reports(handler).list_accounts(CREDENTIALS)
    assert [(a.id, a.name, a.currency) for a in accounts] == [
        (ACCOUNT, "Acme", "EUR"),
        ("7", "7", "USD"),
    ]
    assert accounts[0].details == {"status": "ACTIVE"}
    assert query(seen[0])["q"] == "search"
    assert "tok%2Fen%3D%3D" in str(seen[1].url)


async def test_list_accounts_rejects_an_invalid_element() -> None:
    handler = lambda _: httpx.Response(200, json={"elements": [{"name": "x"}]})  # noqa: E731
    with pytest.raises(ReportError) as caught:
        await reports(handler).list_accounts(CREDENTIALS)
    assert caught.value.reason == "unavailable"


CAMPAIGN = "urn:li:sponsoredCampaign:11"
METRICS = [f"metric{'abcdefghijklmnopqrstuvwxyz'[i]}" for i in range(21)]
SPEC = {"pivot": "CAMPAIGN", "metrics": METRICS}
DAY = date(2026, 11, 3)


async def test_analytics_sends_the_exact_query_and_merges_the_field_chunks() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        fields = query(request)["fields"].split(",")
        row = element(CAMPAIGN, DAY, **{f: "19.91833" for f in fields if f.startswith("metric")})
        return httpx.Response(200, json={"elements": [row]})

    result = await reports(handler).read(CREDENTIALS, ACCOUNT, SPEC, DAY, DAY)
    assert len(seen) == 2
    first = seen[0]
    assert first.headers["Linkedin-Version"] == "202610"
    assert first.headers["X-Restli-Protocol-Version"] == "2.0.0"
    assert "X-RestLi-Method" not in first.headers
    raw = str(first.url)
    assert raw.startswith(
        "https://api.linkedin.com/rest/adAnalytics?q=analytics&pivot=CAMPAIGN&timeGranularity=DAILY"
        "&dateRange=(start:(year:2026,month:11,day:3),end:(year:2026,month:11,day:3))"
        f"&accounts=List(urn%3Ali%3AsponsoredAccount%3A{ACCOUNT})&fields="
    )
    assert query(first)["fields"].split(",") == [*METRICS[:18], "dateRange", "pivotValues"]
    assert query(seen[1])["fields"].split(",") == [*METRICS[18:], "dateRange", "pivotValues"]
    (record,) = result.records
    assert all(record[m] == "19.91833" for m in METRICS)  # money and all, as strings
    assert result.days == [DAY]
    assert result.envelope == {"account": ACCOUNT, "pivot": "CAMPAIGN", "requests": 2}


async def test_the_api_version_option_is_sent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"elements": []})

    spec = {"pivot": "ACCOUNT", "metrics": ["impressions"]}
    await reports(handler, api_version="202701").read(CREDENTIALS, ACCOUNT, spec, DAY, DAY)
    assert seen[0].headers["Linkedin-Version"] == "202701"


async def test_a_long_period_is_read_in_30_day_windows() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"elements": []})

    spec = {"pivot": "ACCOUNT", "metrics": ["impressions"]}
    result = await reports(handler).read(
        CREDENTIALS, ACCOUNT, spec, date(2026, 9, 1), date(2026, 10, 15)
    )
    assert len(seen) == 2 and result.records == []
    assert "start:(year:2026,month:10,day:1)" in str(seen[1].url)


async def test_a_full_response_halves_the_window() -> None:
    seen: list[tuple[date, date]] = []
    start, end = date(2026, 11, 1), date(2026, 11, 4)

    def handler(request: httpx.Request) -> httpx.Response:
        text = str(request.url)
        first = text.split("start:(year:")[1].split(")")[0]
        last = text.split("end:(year:")[1].split(")")[0]

        def parse(part: str) -> date:
            year, month, day = (int(p.split(":")[-1]) for p in part.split(","))
            return date(year, month, day)

        window = (parse(first), parse(last))
        seen.append(window)
        if window == (start, end):
            rows = [
                element(f"urn:li:sponsoredCampaign:{i}", start) for i in range(li.ANALYTICS_LIMIT)
            ]
            return httpx.Response(200, json={"elements": rows})
        return httpx.Response(
            200, json={"elements": [element(CAMPAIGN, window[0], impressions="1")]}
        )

    spec = {"pivot": "CAMPAIGN", "metrics": ["impressions"]}
    result = await reports(handler).read(CREDENTIALS, ACCOUNT, spec, start, end)
    assert seen == [(start, end), (start, date(2026, 11, 2)), (date(2026, 11, 3), end)]
    assert result.days == [start, date(2026, 11, 3)]
    assert result.envelope["requests"] == 3


async def test_a_full_single_day_fails() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        rows = [element(f"urn:li:sponsoredCampaign:{i}", DAY) for i in range(li.ANALYTICS_LIMIT)]
        return httpx.Response(200, json={"elements": rows})

    spec = {"pivot": "CAMPAIGN", "metrics": ["impressions"]}
    with pytest.raises(ReportError) as caught:
        await reports(handler).read(CREDENTIALS, ACCOUNT, spec, DAY, DAY)
    assert caught.value.reason == "request_failed"


def bad_rows() -> list[tuple[str, dict[str, Any]]]:
    multi = element(CAMPAIGN, DAY)
    multi["dateRange"]["end"] = day_of(DAY + timedelta(days=1))
    no_pivot = element(CAMPAIGN, DAY)
    del no_pivot["pivotValues"]
    return [
        ("multi-day", multi),
        ("outside the window", element(CAMPAIGN, DAY + timedelta(days=9))),
        ("no pivotValues", no_pivot),
        ("two pivots", {**element(CAMPAIGN, DAY), "pivotValues": [CAMPAIGN, CAMPAIGN]}),
        ("other entity", element("urn:li:sponsoredCreative:11", DAY)),
        ("not an object", {"x": 1} | {"pivotValues": "nope"}),
    ]


@pytest.mark.parametrize("name,row", bad_rows(), ids=[name for name, _ in bad_rows()])
async def test_a_bad_analytics_row_fails_the_read(name: str, row: dict[str, Any]) -> None:
    handler = lambda _: httpx.Response(200, json={"elements": [row]})  # noqa: E731
    spec = {"pivot": "CAMPAIGN", "metrics": ["impressions"]}
    with pytest.raises(ReportError) as caught:
        await reports(handler).read(CREDENTIALS, ACCOUNT, spec, DAY, DAY)
    assert caught.value.reason == "unavailable"


async def test_a_repeated_row_and_chunks_that_disagree_fail() -> None:
    twice = lambda _: httpx.Response(  # noqa: E731
        200, json={"elements": [element(CAMPAIGN, DAY), element(CAMPAIGN, DAY)]}
    )
    spec = {"pivot": "CAMPAIGN", "metrics": ["impressions"]}
    with pytest.raises(ReportError):
        await reports(twice).read(CREDENTIALS, ACCOUNT, spec, DAY, DAY)

    calls = 0

    def uneven(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        rows = [element(CAMPAIGN, DAY)] if calls == 1 else []
        return httpx.Response(200, json={"elements": rows})

    with pytest.raises(ReportError):
        await reports(uneven).read(CREDENTIALS, ACCOUNT, SPEC, DAY, DAY)


@pytest.mark.parametrize(
    "spec",
    [
        {"pivot": "MEMBER_COMPANY", "metrics": ["clicks"]},
        {"pivot": "CAMPAIGN", "metrics": []},
        {"pivot": "CAMPAIGN", "metrics": ["clicks", "clicks"]},
        {"pivot": "CAMPAIGN", "metrics": ["clicks&accounts=x"]},
        {"pivot": "CAMPAIGN", "metrics": ["dateRange"]},
        {"entity": "members"},
    ],
)
async def test_an_invalid_spec_is_refused_before_any_request(spec: dict[str, Any]) -> None:
    with pytest.raises(ReportError) as caught:
        await reports(lambda _: pytest.fail("no request")).read(
            CREDENTIALS, ACCOUNT, spec, DAY, DAY
        )
    assert caught.value.reason == "request_failed"


@pytest.mark.parametrize("account", ["", "12a", "1&x=2", "urn:li:x"])
async def test_an_account_id_must_be_digits(account: str) -> None:
    with pytest.raises(ReportError) as caught:
        await reports(lambda _: pytest.fail("no request")).read(
            CREDENTIALS, account, SPEC, DAY, DAY
        )
    assert caught.value.reason == "request_failed"


async def test_entities_follow_pages_and_are_labelled_today() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "pageToken" not in query(request):
            body = {"elements": [{"id": 1}], "metadata": {"nextPageToken": "next"}}
        else:
            body = {"elements": [{"id": 2}, {"id": 3}]}
        return httpx.Response(200, json=body)

    result = await reports(handler).read(CREDENTIALS, ACCOUNT, {"entity": "campaigns"}, DAY, DAY)
    assert [r["id"] for r in result.records] == [1, 2, 3]
    assert result.days == [NOW.date()] * 3
    assert result.envelope == {"account": ACCOUNT, "entity": "campaigns", "pages": 2}
    first = str(seen[0].url)
    assert first.startswith(
        f"https://api.linkedin.com/rest/adAccounts/{ACCOUNT}/adCampaigns?q=search"
    )
    assert (
        "search=(status:(values:List(ACTIVE,PAUSED,ARCHIVED,COMPLETED,CANCELED,DRAFT,"
        "PENDING_DELETION,REMOVED)))" in first
    )
    assert "pageSize=1000" in first and "pageToken=next" in str(seen[1].url)
    assert "X-RestLi-Method" not in seen[0].headers


async def test_groups_ask_for_every_status() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"elements": [{"id": 5}]})

    await reports(handler).read(CREDENTIALS, ACCOUNT, {"entity": "campaign_groups"}, DAY, DAY)
    url = str(seen[0].url)
    assert "/adCampaignGroups?q=search" in url
    assert "List(ACTIVE,ARCHIVED,CANCELED,DRAFT,PAUSED,PENDING_DELETION,REMOVED)" in url
    assert "pageSize=1000" in url


async def test_creatives_use_the_finder_header_and_urn_ids() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"elements": [{"id": "urn:li:sponsoredCreative:9"}]})

    result = await reports(handler).read(CREDENTIALS, ACCOUNT, {"entity": "creatives"}, DAY, DAY)
    assert seen[0].headers["X-RestLi-Method"] == "FINDER"
    url = str(seen[0].url)
    assert f"/adAccounts/{ACCOUNT}/creatives?q=criteria" in url and "pageSize=100" in url
    assert result.records == [{"id": "urn:li:sponsoredCreative:9"}]


async def test_an_entity_without_an_id_fails() -> None:
    handler = lambda _: httpx.Response(200, json={"elements": [{"name": "x"}]})  # noqa: E731
    with pytest.raises(ReportError) as caught:
        await reports(handler).read(CREDENTIALS, ACCOUNT, {"entity": "creatives"}, DAY, DAY)
    assert caught.value.reason == "unavailable"


async def test_endless_entity_pages_stop() -> None:
    handler = lambda _: httpx.Response(  # noqa: E731
        200, json={"elements": [{"id": 1}], "metadata": {"nextPageToken": "again"}}
    )
    with pytest.raises(ReportError) as caught:
        await reports(handler).read(CREDENTIALS, ACCOUNT, {"entity": "campaigns"}, DAY, DAY)
    assert caught.value.reason == "request_failed"


@pytest.mark.parametrize("status", [429, 503])
async def test_busy_answers_are_retried_then_unavailable(status: int) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, headers={"Retry-After": "2"})

    with pytest.raises(ReportError) as caught:
        await reports(handler).read(CREDENTIALS, ACCOUNT, {"entity": "campaigns"}, DAY, DAY)
    assert caught.value.reason == "unavailable" and calls == 3


@pytest.mark.parametrize("status", [401, 403])
async def test_a_refused_token_is_auth_failed(status: int) -> None:
    body = {"status": status, "serviceErrorCode": 65601, "message": "REVOKED_ACCESS_TOKEN"}
    with pytest.raises(ReportAuthError) as caught:
        await reports(lambda _: httpx.Response(status, json=body)).read(
            CREDENTIALS, ACCOUNT, {"entity": "campaigns"}, DAY, DAY
        )
    assert caught.value.reason == "auth_failed" and "65601" in caught.value.diagnostics
    no_secret_in(caught.value)


async def test_a_retired_version_is_version_unsupported() -> None:
    with pytest.raises(ReportError) as caught:
        await reports(lambda _: httpx.Response(426, json={"message": "NONEXISTENT_VERSION"})).read(
            CREDENTIALS, ACCOUNT, {"entity": "campaigns"}, DAY, DAY
        )
    assert caught.value.reason == "version_unsupported" and not caught.value.auth


async def test_a_refused_request_carries_linkedins_code_and_no_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = {"status": 400, "serviceErrorCode": 100, "message": "Unknown field metricx"}
    with caplog.at_level(logging.DEBUG), pytest.raises(ReportError) as caught:
        await reports(lambda _: httpx.Response(400, json=body)).read(
            CREDENTIALS, ACCOUNT, SPEC, DAY, DAY
        )
    assert caught.value.reason == "request_failed"
    assert caught.value.diagnostics == "100: Unknown field metricx"
    no_secret_in(caught.value)


def test_classify_accepts_success_and_honours_retry_after() -> None:
    assert classify(httpx.Response(200)) is None
    verdict = classify(httpx.Response(429, headers={"Retry-After": "999"}))
    assert isinstance(verdict, Retry) and verdict.after == li.MAX_RETRY_AFTER
    assert isinstance(classify(httpx.Response(400)), Problem)


async def test_reading_without_an_access_token_is_auth_failed() -> None:
    with pytest.raises(ReportAuthError):
        await reports(lambda _: pytest.fail("no request")).read(
            {}, ACCOUNT, {"entity": "campaigns"}, DAY, DAY
        )
