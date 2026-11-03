"""Microsoft Clarity (Data Export API) against `httpx.MockTransport`."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import httpx
import pytest

from data_sources.config.schema import AuthConfig, ConnectorConfig
from data_sources.reports import ReportAuthError, ReportError, create_report_source
from data_sources.reports.sources import microsoft_clarity as clarity
from data_sources.reports.sources.microsoft_clarity import API_URL, MicrosoftClarityReports

TOKEN = "clarity-token"
CREDENTIALS = {"api_token": TOKEN}
TODAY = date(2026, 11, 2)
SINCE, UNTIL = date(2026, 10, 1), date(2026, 11, 1)

Handler = Callable[[httpx.Request], httpx.Response]

TRAFFIC = {
    "metricName": "Traffic",
    "information": [
        {
            "totalSessionCount": "9554",
            "totalBotSessionCount": "8369",
            "distantUserCount": "189733",
            "PagesPerSessionPercentage": 1.0931,
            "OS": "Other",
        },
        {
            "totalSessionCount": "291942",
            "totalBotSessionCount": "31076",
            "distantUserCount": "212836",
            "PagesPerSessionPercentage": 2.2609,
            "OS": "Android",
        },
    ],
}


async def no_wait(_: float) -> None:
    pass


def reports(handler: Handler) -> MicrosoftClarityReports:
    config = ConnectorConfig(provider="microsoft_clarity", auth=AuthConfig(type="api_key"))
    source = create_report_source(config, transport=httpx.MockTransport(handler), sleep=no_wait)
    assert isinstance(source, MicrosoftClarityReports)
    return source


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clarity, "_today", lambda: TODAY)


async def test_read_sends_the_token_and_dimensions_and_flattens_each_metric() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[TRAFFIC, {"metricName": "ScrollDepth", "information": []}])

    report = await reports(handler).read(
        CREDENTIALS, "project", {"dimensions": ["OS", "Country/Region"]}, SINCE, UNTIL
    )

    (request,) = seen
    assert str(request.url).startswith(API_URL)
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert dict(request.url.params) == {
        "numOfDays": "1",
        "dimension1": "OS",
        "dimension2": "Country/Region",
    }
    assert report.records == [
        {**TRAFFIC["information"][0], "metricName": "Traffic"},
        {**TRAFFIC["information"][1], "metricName": "Traffic"},
    ]
    assert report.days == [TODAY, TODAY]
    assert report.envelope["dimensions"] == ["OS", "Country/Region"]
    assert report.envelope["metrics"] == {"Traffic": 2, "ScrollDepth": 0}
    assert report.envelope["num_of_days"] == 1 and report.envelope["truncated"] == []
    assert TOKEN not in str(report.envelope)


async def test_a_metric_at_the_row_cap_is_marked_truncated() -> None:
    rows = [{"URL": f"https://x/{n}", "visitsCount": "1"} for n in range(clarity.ROW_LIMIT)]

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"metricName": "PopularPages", "information": rows}])

    report = await reports(handler).read(CREDENTIALS, "project", {}, SINCE, UNTIL)
    assert report.envelope["truncated"] == ["PopularPages"]
    assert len(report.records) == clarity.ROW_LIMIT


@pytest.mark.parametrize(
    "spec",
    [
        {"dimensions": ["OS", "Device", "Browser", "URL"]},
        {"dimensions": ["OS", "OS"]},
        {"dimensions": ["Country"]},
        {"num_of_days": 4},
        {"num_of_days": True},
    ],
)
async def test_a_spec_clarity_would_refuse_spends_no_request(spec: dict) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    with pytest.raises(ReportError) as caught:
        await reports(handler).read(CREDENTIALS, "project", spec, SINCE, UNTIL)
    assert caught.value.reason == "request_failed"


@pytest.mark.parametrize("status", [401, 403])
async def test_a_refused_token_is_an_auth_failure(status: int) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    with pytest.raises(ReportAuthError) as caught:
        await reports(handler).read(CREDENTIALS, "project", {}, SINCE, UNTIL)
    assert caught.value.reason == "auth_failed" and caught.value.auth


async def test_the_daily_limit_is_not_retried() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, text="Exceeded daily limit")

    with pytest.raises(ReportError) as caught:
        await reports(handler).read(CREDENTIALS, "project", {}, SINCE, UNTIL)
    assert caught.value.reason == "rate_limited" and calls == 1


async def test_a_server_error_is_tried_once_more() -> None:
    answers = iter([httpx.Response(503), httpx.Response(200, json=[TRAFFIC])])

    report = await reports(lambda _: next(answers)).read(CREDENTIALS, "project", {}, SINCE, UNTIL)
    assert len(report.records) == 2


async def test_a_bad_request_keeps_what_clarity_said() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="Invalid   dimension")

    with pytest.raises(ReportError) as caught:
        await reports(handler).read(CREDENTIALS, "project", {}, SINCE, UNTIL)
    assert caught.value.reason == "request_failed"
    assert caught.value.diagnostics == "HTTP 400: Invalid dimension"


@pytest.mark.parametrize(
    "body",
    [{"metricName": "Traffic"}, [{"information": []}], [{"metricName": "T", "information": [1]}]],
)
async def test_an_unexpected_body_is_invalid(body: object) -> None:
    with pytest.raises(ReportError) as caught:
        await reports(lambda _: httpx.Response(200, json=body)).read(
            CREDENTIALS, "project", {}, SINCE, UNTIL
        )
    assert caught.value.reason == "unavailable"


async def test_no_token_is_an_auth_failure_without_a_request() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    with pytest.raises(ReportAuthError):
        await reports(handler).read({"api_token": " "}, "project", {}, SINCE, UNTIL)


async def test_the_one_account_is_the_tokens_project_without_a_request() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    (account,) = await reports(handler).list_accounts(CREDENTIALS)
    assert account.id == "project"


async def test_there_is_no_oauth() -> None:
    source = reports(lambda _: httpx.Response(500))
    with pytest.raises(ReportError):
        source.authorize_url("state")
    with pytest.raises(ReportError):
        await source.exchange_code("code")
