from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import zipfile
from collections.abc import AsyncIterator, Callable
from datetime import date
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlsplit

import httpx
import pytest

from data_sources.config.schema import AuthConfig, ConnectorConfig
from data_sources.reports import ReportAuthError, ReportError, create_report_source
from data_sources.reports.sources import microsoft_ads
from data_sources.reports.sources.microsoft_ads import (
    ACCOUNT_PAGE_SIZE,
    MicrosoftAdsReports,
)

CLIENT_ID = "client-id"
CLIENT_SECRET = "client-secret"
DEVELOPER_TOKEN = "dev-token"
REDIRECT = "https://app.example/api/v1/oauth/microsoft/callback"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
REPORTING = "https://reporting.api.bingads.microsoft.com/Reporting/v13/"
CLIENTCENTER = "https://clientcenter.api.bingads.microsoft.com/CustomerManagement/v13/"
LINK = "https://bingadsappsstorage.blob.core.windows.net/r/report.zip?sv=2020&sig=SECRET-SIG"
CREDENTIALS = {"refresh_token": "r1", "scope": microsoft_ads.SCOPE}
SPEC = {
    "report": "CampaignPerformanceReportRequest",
    "attributes": ["AccountId", "CampaignId", "CampaignName"],
    "metrics": ["Clicks", "Ctr"],
    "day_column": "TimePeriod",
}
COLUMNS = ["TimePeriod", "AccountId", "CampaignId", "CampaignName", "Clicks", "Ctr"]

Handler = Callable[[httpx.Request], httpx.Response]


class Sleeps:
    """A sleep that records how long it was asked to wait, and doesn't."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class Recorder:
    """A `MockTransport` handler that keeps every request it was sent."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def reports(handler: Handler, sleep: Sleeps | None = None, **options: Any) -> MicrosoftAdsReports:
    credentials = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "developer_token": DEVELOPER_TOKEN,
        "redirect_uri": REDIRECT,
    }
    config = ConnectorConfig(
        provider="microsoft_ads",
        auth=AuthConfig(type="oauth2", credentials=credentials),
        options=options,
    )
    source = create_report_source(
        config, transport=httpx.MockTransport(handler), sleep=sleep or Sleeps()
    )
    assert isinstance(source, MicrosoftAdsReports)
    return source


def no_request(_: httpx.Request) -> httpx.Response:
    pytest.fail("no request expected")


def id_token(**claims: str) -> str:
    def part(value: dict[str, str]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


def form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode())


def token_ok(request: httpx.Request) -> httpx.Response:
    assert str(request.url) == TOKEN_URL
    return httpx.Response(200, json={"access_token": "access-1", "refresh_token": "r2"})


def zipped(*files: tuple[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files:
            archive.writestr(name, content)
    return buffer.getvalue()


CSV = (
    '"TimePeriod","AccountId","CampaignId","CampaignName","Clicks","Ctr"\r\n'
    '"2026-10-01","111","9","Brand, exact","3","1.50%"\r\n'
    '"2026-10-03","111","9","Brand, exact","4","2.00%"\r\n'
).encode("utf-8-sig")


def report_handler(statuses: list[dict[str, Any]], download: Handler | None = None) -> Recorder:
    polls = iter(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == TOKEN_URL:
            return token_ok(request)
        if url == f"{REPORTING}GenerateReport/Submit":
            return httpx.Response(200, json={"ReportRequestId": "r1"})
        if url == f"{REPORTING}GenerateReport/Poll":
            assert json.loads(request.content) == {"ReportRequestId": "r1"}
            return httpx.Response(200, json={"ReportRequestStatus": next(polls)})
        if url == LINK:
            if download is not None:
                return download(request)
            return httpx.Response(200, content=zipped(("report.csv", CSV)))
        pytest.fail(f"unexpected request {request.method} {url}")

    return Recorder(handler)


PENDING = {"Status": "Pending", "ReportDownloadUrl": None}
DONE = {"Status": "Success", "ReportDownloadUrl": LINK}


# --- consent -----------------------------------------------------------------------------------


def test_authorize_url() -> None:
    url = reports(no_request).authorize_url("st")
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    )
    query = dict(parse_qsl(parts.query))
    assert query["client_id"] == CLIENT_ID and query["response_type"] == "code"
    assert query["redirect_uri"] == REDIRECT and query["response_mode"] == "query"
    assert query["state"] == "st" and query["prompt"] == "select_account"
    assert {"https://ads.microsoft.com/msads.manage", "offline_access"} <= set(
        query["scope"].split()
    )
    assert CLIENT_SECRET not in url and DEVELOPER_TOKEN not in url


def test_a_tenant_option_goes_into_the_url_and_a_bad_one_is_refused() -> None:
    url = reports(no_request, tenant="contoso.onmicrosoft.com").authorize_url("st")
    assert url.startswith(
        "https://login.microsoftonline.com/contoso.onmicrosoft.com/oauth2/v2.0/authorize?"
    )
    with pytest.raises(ReportError) as caught:
        reports(no_request, tenant="evil/../x").authorize_url("st")
    assert caught.value.reason == "oauth_not_configured"


async def test_exchange_code_posts_a_form_and_names_who_consented() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and str(request.url) == TOKEN_URL
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        assert form(request) == {
            "grant_type": ["authorization_code"],
            "code": ["c"],
            "redirect_uri": [REDIRECT],
            "client_id": [CLIENT_ID],
            "client_secret": [CLIENT_SECRET],
            "scope": ["https://ads.microsoft.com/msads.manage offline_access"],
        }
        return httpx.Response(
            200,
            json={
                "access_token": "a",
                "refresh_token": "r1",
                "scope": "https://ads.microsoft.com/msads.manage offline_access",
                "id_token": id_token(preferred_username="a@outlook.com"),
            },
        )

    grant = await reports(handler).exchange_code("c")
    assert grant.credentials == {
        "refresh_token": "r1",
        "scope": "https://ads.microsoft.com/msads.manage offline_access",
    }
    assert grant.identity == "a@outlook.com"
    assert "r1" not in repr(grant)


async def test_the_email_claim_wins_over_the_username() -> None:
    body = {"refresh_token": "r1", "id_token": id_token(email="e@x.ro", preferred_username="u")}
    grant = await reports(lambda _: httpx.Response(200, json=body)).exchange_code("c")
    assert grant.identity == "e@x.ro"


@pytest.mark.parametrize(
    ("response", "reason", "auth"),
    [
        (httpx.Response(200, json={"access_token": "a"}), "no_refresh_token", False),
        (
            httpx.Response(200, json={"refresh_token": "r1", "scope": "openid email"}),
            "access_denied",
            False,
        ),
        (
            httpx.Response(
                400,
                json={
                    "error": "invalid_grant",
                    "error_description": "AADSTS70008: code c expired",
                    "error_codes": [70008],
                },
            ),
            "auth_failed",
            True,
        ),
        (httpx.Response(503, text="down"), "unavailable", False),
    ],
)
async def test_exchange_code_failures(response: httpx.Response, reason: str, auth: bool) -> None:
    recorder = Recorder(lambda _: response)
    with pytest.raises(ReportError) as caught:
        await reports(recorder).exchange_code("c")
    assert caught.value.reason == reason and caught.value.auth is auth
    assert isinstance(caught.value, ReportAuthError) is auth
    assert CLIENT_SECRET not in str(caught.value) and "AADSTS70008: code" not in str(caught.value)
    assert "c expired" not in caught.value.diagnostics
    assert len(recorder.requests) == 1  # a code works once: never retried
    if auth:
        assert caught.value.diagnostics == "invalid_grant (AADSTS70008)"


# --- refresh tokens rotate ---------------------------------------------------------------------


async def test_refresh_returns_the_newest_refresh_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert form(request)["grant_type"] == ["refresh_token"]
        assert form(request)["refresh_token"] == ["r1"]
        return httpx.Response(200, json={"access_token": "a", "refresh_token": "r2"})

    grant = await reports(handler).refresh_grant(CREDENTIALS)
    assert grant.credentials == {"refresh_token": "r2", "scope": microsoft_ads.SCOPE}


async def test_refresh_keeps_the_stored_token_when_none_comes_back() -> None:
    body = {"access_token": "a"}
    grant = await reports(lambda _: httpx.Response(200, json=body)).refresh_grant(CREDENTIALS)
    assert grant.credentials["refresh_token"] == "r1"


async def test_a_refresh_token_that_stopped_working_is_an_auth_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(ReportAuthError) as caught:
        await reports(handler).refresh_grant(CREDENTIALS)
    assert caught.value.auth and caught.value.reason == "auth_failed"
    with pytest.raises(ReportAuthError):
        await reports(no_request).refresh_grant({})


# --- accounts ----------------------------------------------------------------------------------


def account(index: int) -> dict[str, Any]:
    return {
        # Longs as numbers and as strings: both are read.
        "Id": index if index % 2 else str(index),
        "Name": f"Account {index}",
        "Number": f"F{index}",
        "ParentCustomerId": "222" if index % 2 else 222,
        "CurrencyCode": "USD",
        "TimeZone": "AmsterdamBerlinBernRomeStockholmVienna",
        "AccountLifeCycleStatus": "Active",
    }


async def test_accounts_are_every_page_of_the_users_accounts() -> None:
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == TOKEN_URL:
            return token_ok(request)
        assert request.headers["Authorization"] == "Bearer access-1"
        assert request.headers["DeveloperToken"] == DEVELOPER_TOKEN
        assert "CustomerAccountId" not in request.headers
        body = json.loads(request.content)
        if url == f"{CLIENTCENTER}User/Query":
            assert body == {"UserId": None}
            return httpx.Response(200, json={"User": {"Id": 42, "UserName": "u"}})
        assert url == f"{CLIENTCENTER}Accounts/Search"
        assert body["Predicates"] == [{"Field": "UserId", "Operator": "Equals", "Value": "42"}]
        assert body["PageInfo"]["Size"] == ACCOUNT_PAGE_SIZE
        index = body["PageInfo"]["Index"]
        pages.append(index)
        count = ACCOUNT_PAGE_SIZE if index == 0 else 3
        start = index * ACCOUNT_PAGE_SIZE + 1
        return httpx.Response(
            200, json={"Accounts": [account(i) for i in range(start, start + count)]}
        )

    found = await reports(handler).list_accounts(CREDENTIALS)
    assert pages == [0, 1] and len(found) == ACCOUNT_PAGE_SIZE + 3
    first = found[0]
    assert first.id == "1:222" and first.name == "Account 1" and first.currency == "USD"
    assert first.timezone == "AmsterdamBerlinBernRomeStockholmVienna"
    assert first.details == {
        "account_id": "1",
        "customer_id": "222",
        "number": "F1",
        "status": "Active",
    }
    assert found[1].id == "2:222"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"Accounts": None},
        {"Accounts": [None]},
        {"Accounts": [{"Id": "x", "ParentCustomerId": 1}]},
    ],
)
async def test_a_malformed_account_list_is_an_error_not_an_empty_list(body: dict[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return token_ok(request)
        if request.url.path.endswith("User/Query"):
            return httpx.Response(200, json={"User": {"Id": "42"}})
        return httpx.Response(200, json=body)

    with pytest.raises(ReportError) as caught:
        await reports(handler).list_accounts(CREDENTIALS)
    assert caught.value.reason == "unavailable"


# --- reports -----------------------------------------------------------------------------------


async def test_a_report_is_submitted_polled_downloaded_and_read() -> None:
    sleeps = Sleeps()
    recorder = report_handler([PENDING, PENDING, DONE])
    report = await reports(recorder, sleeps).read(
        CREDENTIALS, "111:222", SPEC, date(2026, 10, 1), date(2026, 10, 3)
    )
    submit = next(r for r in recorder.requests if r.url.path.endswith("Submit"))
    assert json.loads(submit.content) == {
        "ReportRequest": {
            "Type": "CampaignPerformanceReportRequest",
            "Aggregation": "Daily",
            "Format": "Csv",
            "FormatVersion": "2.0",
            "ExcludeReportHeader": True,
            "ExcludeReportFooter": True,
            "ExcludeColumnHeaders": False,
            "ReturnOnlyCompleteData": False,
            "Columns": COLUMNS,
            "Scope": {"AccountIds": ["111"]},
            "Time": {
                "CustomDateRangeStart": {"Day": 1, "Month": 10, "Year": 2026},
                "CustomDateRangeEnd": {"Day": 3, "Month": 10, "Year": 2026},
            },
        }
    }
    for request in recorder.requests:
        if request.url.host.startswith("reporting"):
            assert request.headers["CustomerId"] == "222"
            assert request.headers["CustomerAccountId"] == "111"
            assert request.headers["DeveloperToken"] == DEVELOPER_TOKEN
            assert request.headers["Authorization"] == "Bearer access-1"
    assert report.records == [
        {
            "TimePeriod": "2026-10-01",
            "AccountId": "111",
            "CampaignId": "9",
            "CampaignName": "Brand, exact",
            "Clicks": "3",
            "Ctr": "1.50%",
        },
        {
            "TimePeriod": "2026-10-03",
            "AccountId": "111",
            "CampaignId": "9",
            "CampaignName": "Brand, exact",
            "Clicks": "4",
            "Ctr": "2.00%",
        },
    ]
    assert report.days == [date(2026, 10, 1), date(2026, 10, 3)]
    assert sleeps.calls == [2, 4]
    assert report.envelope == {
        "report": "CampaignPerformanceReportRequest",
        "columns": COLUMNS,
        "account_id": "111",
        "customer_id": "222",
    }
    assert "SECRET-SIG" not in json.dumps(report.envelope) and "r1" not in report.envelope.values()


async def test_a_report_built_with_no_link_is_empty() -> None:
    recorder = report_handler([{"Status": "Success", "ReportDownloadUrl": None}])
    report = await reports(recorder).read(
        CREDENTIALS, "111:222", SPEC, date(2026, 10, 1), date(2026, 10, 1)
    )
    assert report.records == [] and report.days == []
    assert not any(str(r.url) == LINK for r in recorder.requests)


async def test_a_report_microsoft_could_not_build_fails() -> None:
    recorder = report_handler([PENDING, {"Status": "Error", "ReportDownloadUrl": None}])
    with pytest.raises(ReportError) as caught:
        await reports(recorder).read(
            CREDENTIALS, "111:222", SPEC, date(2026, 10, 1), date(2026, 10, 1)
        )
    assert caught.value.reason == "request_failed" and "Error" in caught.value.diagnostics


async def test_a_report_that_is_never_ready_times_out() -> None:
    sleeps = Sleeps()
    recorder = report_handler([PENDING] * 100)
    with pytest.raises(ReportError) as caught:
        await reports(recorder, sleeps, report_timeout=60).read(
            CREDENTIALS, "111:222", SPEC, date(2026, 10, 1), date(2026, 10, 1)
        )
    assert caught.value.reason == "report_timeout" and not caught.value.auth
    # 2, 4, 8, 16, then what is left of the 60 s (a hair under 30: requests take time too).
    assert sleeps.calls[:4] == [2, 4, 8, 16] and len(sleeps.calls) == 5
    assert 59 < sum(sleeps.calls) <= 60
    polls = [r for r in recorder.requests if r.url.path.endswith("Poll")]
    assert len(polls) == len(sleeps.calls) + 1


async def test_slow_requests_use_up_the_report_budget_too(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(microsoft_ads, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    sleeps = Sleeps()
    inner = report_handler([PENDING] * 100)

    def slow(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("Poll"):
            clock[0] += 20  # each poll takes 20 s
        return inner(request)

    with pytest.raises(ReportError) as caught:
        await reports(slow, sleeps, report_timeout=60).read(
            CREDENTIALS, "111:222", SPEC, date(2026, 10, 1), date(2026, 10, 1)
        )
    assert caught.value.reason == "report_timeout"
    # 20 s + 2 + 20 s + 4 + 20 s: the budget runs out after two sleeps, not five.
    assert sleeps.calls == [2, 4] and sum(sleeps.calls) < 60


async def test_a_download_that_never_ends_times_out_as_a_report_timeout() -> None:
    async def hang(_: float) -> None:
        await asyncio.Event().wait()

    class Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.host.endswith("windows.net"):
                await asyncio.Event().wait()  # the download never answers
            return report_handler([DONE])(request)

    config = ConnectorConfig(
        provider="microsoft_ads",
        auth=AuthConfig(
            type="oauth2",
            credentials={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "developer_token": DEVELOPER_TOKEN,
                "redirect_uri": REDIRECT,
            },
        ),
        options={"report_timeout": 0.2},
    )
    source = create_report_source(config, transport=Slow(), sleep=hang)
    with pytest.raises(ReportError) as caught:
        await source.read(CREDENTIALS, "111:222", SPEC, date(2026, 10, 1), date(2026, 10, 1))
    assert caught.value.reason == "report_timeout"


def fault(status: int, *errors: dict[str, Any], key: str = "OperationErrors") -> httpx.Response:
    return httpx.Response(
        status, json={"TrackingId": "track-1", "Type": "ApiFault", key: list(errors)}
    )


@pytest.mark.parametrize(
    "response",
    [
        fault(401, {"Code": 105, "ErrorCode": "InvalidCredentials", "Message": "no"}),
        fault(400, {"Code": 109, "ErrorCode": "AuthenticationTokenExpired", "Message": "x"}),
        fault(400, {"Code": "106", "ErrorCode": "UserIsNotAuthorized"}, key="Errors"),
        httpx.Response(401, text="unauthorized"),
    ],
)
async def test_auth_faults(response: httpx.Response) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return token_ok(request) if str(request.url) == TOKEN_URL else response

    with pytest.raises(ReportAuthError):
        await reports(handler).read(
            CREDENTIALS, "111:222", SPEC, date(2026, 10, 1), date(2026, 10, 1)
        )


@pytest.mark.parametrize(
    "transient", [fault(400, {"Code": 117, "ErrorCode": "CallRateExceeded"}), httpx.Response(500)]
)
async def test_transient_faults_are_retried(transient: httpx.Response) -> None:
    submits: list[int] = []
    sleeps = Sleeps()
    inner = report_handler([DONE])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("Submit"):
            submits.append(1)
            if len(submits) == 1:
                return transient
        return inner(request)

    report = await reports(handler, sleeps).read(
        CREDENTIALS, "111:222", SPEC, date(2026, 10, 1), date(2026, 10, 3)
    )
    assert len(submits) == 2 and len(report.records) == 2 and sleeps.calls == [0.5]


async def test_a_fault_that_stays_transient_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return token_ok(request)
        return fault(400, {"Code": 207, "ErrorCode": "Busy"})

    with pytest.raises(ReportError) as caught:
        await reports(handler).read(
            CREDENTIALS, "111:222", SPEC, date(2026, 10, 1), date(2026, 10, 1)
        )
    assert caught.value.reason == "unavailable"


async def test_a_refused_report_says_what_microsoft_said_without_credentials() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return token_ok(request)
        return fault(
            400,
            {
                "Code": 2015,
                "ErrorCode": "ReportingServiceRequiredColumnsNotSelected",
                "Message": "Select the required columns.",
            },
        )

    with pytest.raises(ReportError) as caught:
        await reports(handler).read(
            CREDENTIALS, "111:222", SPEC, date(2026, 10, 1), date(2026, 10, 1)
        )
    error = caught.value
    assert error.reason == "request_failed" and not error.auth
    assert error.diagnostics == (
        "ReportingServiceRequiredColumnsNotSelected: Select the required columns. "
        "(tracking track-1)"
    )
    for secret in ("access-1", DEVELOPER_TOKEN, CLIENT_SECRET, "r1"):
        assert secret not in str(error) and secret not in error.diagnostics


# --- the download link is a credential ---------------------------------------------------------


async def test_the_download_sends_no_credentials() -> None:
    recorder = report_handler([DONE])
    await reports(recorder).read(CREDENTIALS, "111:222", SPEC, date(2026, 10, 1), date(2026, 10, 3))
    [download] = [r for r in recorder.requests if str(r.url) == LINK]
    for header in ("Authorization", "DeveloperToken", "CustomerId", "CustomerAccountId"):
        assert header not in download.headers


async def test_a_failed_download_never_shows_the_link(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    with pytest.raises(ReportError) as caught:
        await reports(report_handler([DONE], broken)).read(
            CREDENTIALS, "111:222", SPEC, date(2026, 10, 1), date(2026, 10, 1)
        )
    assert caught.value.reason == "unavailable"
    assert "SECRET-SIG" not in str(caught.value) and "SECRET-SIG" not in caught.value.diagnostics
    assert caught.value.__cause__ is None and "SECRET-SIG" not in caplog.text


async def test_a_successful_download_is_not_logged_with_its_link(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="httpx")
    await reports(report_handler([DONE])).read(
        CREDENTIALS, "111:222", SPEC, date(2026, 10, 1), date(2026, 10, 3)
    )
    assert "GenerateReport/Submit" in caplog.text  # httpx does log requests...
    assert "SECRET-SIG" not in caplog.text and "[report download link]" in caplog.text


@pytest.mark.parametrize(
    "link",
    [
        "https://BingAdsAppsStorage.blob.core.windows.net:443/r/report.zip?sv=2020&sig=SECRET-SIG",
        "https://bingadsappsstorage.blob.core.windows.net/r/report.zip?sig=SECRET-SIG&x=a b",
    ],
)
async def test_a_link_httpx_rewrites_is_still_kept_out_of_the_log(
    caplog: pytest.LogCaptureFixture, link: str
) -> None:
    caplog.set_level(logging.INFO, logger="httpx")
    inner = report_handler([{"Status": "Success", "ReportDownloadUrl": link}])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "bingadsappsstorage.blob.core.windows.net":
            assert str(request.url) != link  # httpx rewrote it before logging it
            return httpx.Response(200, content=zipped(("report.csv", CSV)))
        return inner(request)

    report = await reports(handler).read(
        CREDENTIALS, "111:222", SPEC, date(2026, 10, 1), date(2026, 10, 3)
    )
    assert len(report.records) == 2
    assert "SECRET-SIG" not in caplog.text and "[report download link]" in caplog.text


async def test_a_download_too_large_is_refused_while_it_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(microsoft_ads, "MAX_REPORT_BYTES", 1000)
    sent: list[int] = []

    async def body() -> AsyncIterator[bytes]:
        for _ in range(100):
            sent.append(1)
            yield b"x" * 100

    recorder = report_handler([DONE], lambda _: httpx.Response(200, content=body()))
    with pytest.raises(ReportError) as caught:
        await reports(recorder).read(
            CREDENTIALS, "111:222", SPEC, date(2026, 10, 1), date(2026, 10, 1)
        )
    assert caught.value.reason == "request_failed" and "too large" in str(caught.value)
    assert len(sent) <= 11  # stopped as soon as the cap was passed, not after all 100 chunks


async def test_an_expired_link_is_unavailable() -> None:
    recorder = report_handler([DONE], lambda _: httpx.Response(403, text="AuthenticationFailed"))
    with pytest.raises(ReportError) as caught:
        await reports(recorder).read(
            CREDENTIALS, "111:222", SPEC, date(2026, 10, 1), date(2026, 10, 1)
        )
    assert caught.value.reason == "unavailable" and "expired" in str(caught.value)


@pytest.mark.parametrize("link", ["http://plain.example/r.zip", "file:///etc/passwd", 7])
async def test_only_https_links_are_followed(link: object) -> None:
    recorder = report_handler([{"Status": "Success", "ReportDownloadUrl": link}])
    with pytest.raises(ReportError) as caught:
        await reports(recorder).read(
            CREDENTIALS, "111:222", SPEC, date(2026, 10, 1), date(2026, 10, 1)
        )
    assert caught.value.reason == "unavailable"
    assert all(
        r.url.host.endswith(("microsoft.com", "microsoftonline.com")) for r in recorder.requests
    )


# --- the zip is untrusted ----------------------------------------------------------------------


async def test_a_csv_that_inflates_too_far_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(microsoft_ads, "MAX_REPORT_BYTES", 4096)
    monkeypatch.setattr(microsoft_ads, "_CHUNK", 1024)
    big = CSV + b"x" * 1_000_000  # compresses to far less than the cap

    content = zipped(("report.csv", big))
    assert len(content) < 4096
    recorder = report_handler([DONE], lambda _: httpx.Response(200, content=content))
    with pytest.raises(ReportError) as caught:
        await reports(recorder).read(
            CREDENTIALS, "111:222", SPEC, date(2026, 10, 1), date(2026, 10, 1)
        )
    assert caught.value.reason == "request_failed" and "too large" in str(caught.value)


@pytest.mark.parametrize(
    "content",
    [
        zipped(),
        zipped(("a.csv", CSV), ("b.csv", CSV)),
        CSV,
        zipped(("report.csv", b'"TimePeriod","Other"\r\n"2026-10-01","x"\r\n')),
        zipped(("report.csv", CSV + b'"2026-10-01","111","9","n","3","1%","extra"\r\n')),
        zipped(("report.csv", b"\xff\xfe\x00bad")),
    ],
    ids=["no-file", "two-files", "not-a-zip", "missing-columns", "extra-values", "not-utf8"],
)
async def test_a_malformed_download_is_invalid(content: bytes) -> None:
    recorder = report_handler([DONE], lambda _: httpx.Response(200, content=content))
    with pytest.raises(ReportError) as caught:
        await reports(recorder).read(
            CREDENTIALS, "111:222", SPEC, date(2026, 10, 1), date(2026, 10, 1)
        )
    assert caught.value.reason == "unavailable"


async def test_a_report_with_only_headers_has_no_rows() -> None:
    header = b'"TimePeriod","AccountId","CampaignId","CampaignName","Clicks","Ctr"\r\n'
    recorder = report_handler(
        [DONE], lambda _: httpx.Response(200, content=zipped(("r.csv", header)))
    )
    report = await reports(recorder).read(
        CREDENTIALS, "111:222", SPEC, date(2026, 10, 1), date(2026, 10, 1)
    )
    assert report.records == [] and report.days == []


# --- environments and definitions --------------------------------------------------------------


async def test_the_sandbox_uses_the_sandbox_hosts() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        path = request.url.path
        if str(request.url) == TOKEN_URL:
            return token_ok(request)
        if path.endswith("User/Query"):
            return httpx.Response(200, json={"User": {"Id": 1}})
        if path.endswith("Accounts/Search"):
            return httpx.Response(200, json={"Accounts": []})
        if path.endswith("Submit"):
            return httpx.Response(200, json={"ReportRequestId": "r1"})
        return httpx.Response(
            200, json={"ReportRequestStatus": {"Status": "Success", "ReportDownloadUrl": None}}
        )

    sandbox = reports(handler, environment="sandbox")
    assert await sandbox.list_accounts(CREDENTIALS) == []
    await sandbox.read(CREDENTIALS, "111:222", SPEC, date(2026, 10, 1), date(2026, 10, 1))
    assert set(hosts) == {
        "login.microsoftonline.com",
        "clientcenter.api.sandbox.bingads.microsoft.com",
        "reporting.api.sandbox.bingads.microsoft.com",
    }
    with pytest.raises(ReportError) as caught:
        await reports(no_request, environment="staging").list_accounts(CREDENTIALS)
    assert caught.value.reason == "oauth_not_configured"


@pytest.mark.parametrize(
    ("account", "spec"),
    [
        ("111:222", {**SPEC, "report": "X; DROP"}),
        ("111:222", {**SPEC, "report": "CampaignPerformance"}),
        ("111:222", {**SPEC, "metrics": ["Spend,Clicks"]}),
        ("111:222", {**SPEC, "metrics": []}),
        ("111:222", {**SPEC, "attributes": "AccountId"}),
        ("111:222", {**SPEC, "day_column": None}),
        ("111:222", {**SPEC, "metrics": ["Clicks", "Clicks"]}),
        ("111", SPEC),
        ("111:x", SPEC),
    ],
)
async def test_definitions_and_accounts_are_checked_before_any_request(
    account: str, spec: dict[str, Any]
) -> None:
    with pytest.raises(ReportError) as caught:
        await reports(no_request).read(
            CREDENTIALS, account, spec, date(2026, 10, 1), date(2026, 10, 1)
        )
    assert caught.value.reason == "request_failed"


def test_configured_needs_the_developer_token() -> None:
    def source(**credentials: str) -> bool:
        config = ConnectorConfig(
            provider="microsoft_ads", auth=AuthConfig(type="oauth2", credentials=credentials)
        )
        return create_report_source(config).configured

    assert source(client_id="c", client_secret="s", developer_token="d")
    assert not source(client_id="c", client_secret="s")
