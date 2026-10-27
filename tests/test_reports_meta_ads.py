from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import date
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlsplit

import httpx
import pytest

from data_sources.config.schema import AuthConfig, ConnectorConfig
from data_sources.reports import Grant, ReportAuthError, ReportError, create_report_source
from data_sources.reports.sources.meta_ads import (
    AdAccount,
    CustomConversion,
    MetaAdsReports,
    appsecret_proof,
)

APP_ID = "app-1"
APP_SECRET = "app-secret"
REDIRECT = "https://app.example/api/v1/oauth/meta/callback"
TOKEN = "long-lived-token"
ACCOUNT = "act_123"

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


def config(app_secret: str = APP_SECRET, **options: str) -> ConnectorConfig:
    credentials = {"app_id": APP_ID, "app_secret": app_secret, "redirect_uri": REDIRECT}
    return ConnectorConfig(
        provider="meta_ads",
        auth=AuthConfig(type="oauth2", credentials=credentials),
        options=options,
    )


def reports(handler: Handler, sleep: Sleeps | None = None) -> MetaAdsReports:
    source = create_report_source(
        config(), transport=httpx.MockTransport(handler), sleep=sleep or Sleeps()
    )
    assert isinstance(source, MetaAdsReports)
    return source


def graph_error(
    code: int, status: int = 400, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(
        status, json={"error": {"code": code, "message": f"secret {TOKEN}"}}, headers=headers
    )


def usage(minutes: int) -> dict[str, str]:
    entry = {"type": "ads_insights", "call_count": 100, "estimated_time_to_regain_access": minutes}
    return {"x-business-use-case-usage": json.dumps({"123": [entry]})}


def ok(data: list[dict[str, Any]], **extra: Any) -> httpx.Response:
    return httpx.Response(200, json={"data": data, **extra})


def path(request: httpx.Request) -> str:
    assert request.url.host == "graph.facebook.com"
    assert request.url.path.startswith("/v26.0/")
    return request.url.path.removeprefix("/v26.0/")


def assert_signed(request: httpx.Request) -> None:
    """The token travels in the Authorization header only, with `appsecret_proof` beside it."""
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(request.url)
    assert "access_token" not in request.url.params
    assert request.url.params["appsecret_proof"] == appsecret_proof(TOKEN, APP_SECRET)


# --- consent -----------------------------------------------------------------------------------


def test_authorize_url_names_the_app_redirect_state_and_scope() -> None:
    url = reports(lambda _: httpx.Response(500)).authorize_url("signed-state")
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
        "https://www.facebook.com/v26.0/dialog/oauth"
    )
    assert dict(parse_qsl(parts.query)) == {
        "client_id": APP_ID,
        "redirect_uri": REDIRECT,
        "state": "signed-state",
        "scope": "ads_read",
        "response_type": "code",
    }
    assert APP_SECRET not in url


def test_authorize_url_follows_the_api_version() -> None:
    client = MetaAdsReports(config(api_version="v27.0"))
    assert client.authorize_url("s").startswith("https://www.facebook.com/v27.0/dialog/oauth?")


def test_appsecret_proof_is_hmac_sha256_of_the_token() -> None:
    expected = hmac.new(APP_SECRET.encode(), TOKEN.encode(), hashlib.sha256).hexdigest()
    assert appsecret_proof(TOKEN, APP_SECRET) == expected


# --- exchange_code -----------------------------------------------------------------------------


async def test_exchange_code_trades_the_code_then_extends_the_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert path(request) == "me" and request.headers["Authorization"] == f"Bearer {TOKEN}"
            return httpx.Response(200, json={"name": "Ana"})
        assert path(request) == "oauth/access_token"
        assert request.url.query == b""
        form = parse_qs(request.content.decode())
        if "code" in form:
            assert form == {
                "client_id": [APP_ID],
                "client_secret": [APP_SECRET],
                "redirect_uri": [REDIRECT],
                "code": ["consent-code"],
            }
            return httpx.Response(200, json={"access_token": "short", "token_type": "bearer"})
        assert form == {
            "client_id": [APP_ID],
            "client_secret": [APP_SECRET],
            "grant_type": ["fb_exchange_token"],
            "fb_exchange_token": ["short"],
        }
        return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 5183944})

    recorder = Recorder(handler)
    token = await reports(recorder).exchange_code("consent-code")
    assert token == Grant({"access_token": TOKEN}, identity="Ana", expires_in=5183944)
    assert len(recorder.requests) == 3
    for request in recorder.requests:
        for secret in (APP_SECRET, "consent-code", "short", TOKEN):
            assert secret not in str(request.url)


async def test_exchange_code_without_expiry() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": TOKEN})

    assert await reports(handler).exchange_code("c") == Grant({"access_token": TOKEN})


def test_grant_repr_hides_the_token() -> None:
    grant = Grant({"access_token": TOKEN}, identity="Ana", expires_in=60)
    assert TOKEN not in repr(grant)
    assert "60" in repr(grant)


async def test_refused_code_is_auth_failed() -> None:
    recorder = Recorder(lambda _: graph_error(100, 400))
    with pytest.raises(ReportAuthError) as caught:
        await reports(recorder).exchange_code("bad-code")
    assert caught.value.reason == "auth_failed" and caught.value.auth
    assert len(recorder.requests) == 1
    assert TOKEN not in str(caught.value)


async def test_refused_extension_is_auth_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "fb_exchange_token" in parse_qs(request.content.decode()):
            return graph_error(190, 400)
        return httpx.Response(200, json={"access_token": "short"})

    recorder = Recorder(handler)
    with pytest.raises(ReportAuthError) as caught:
        await reports(recorder).exchange_code("c")
    assert caught.value.reason == "auth_failed"
    assert len(recorder.requests) == 2


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"token_type": "bearer"}),
        httpx.Response(200, json={"access_token": ""}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=["access_token"]),
    ],
)
async def test_exchange_without_a_token_is_auth_failed(response: httpx.Response) -> None:
    with pytest.raises(ReportAuthError) as caught:
        await reports(lambda _: response).exchange_code("c")
    assert caught.value.reason == "auth_failed"


async def test_exchange_network_error_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed {request.url}", request=request)

    with pytest.raises(ReportError) as caught:
        await reports(handler).exchange_code("consent-code")
    assert caught.value.reason == "unavailable" and not caught.value.auth
    assert "consent-code" not in str(caught.value)


# --- user_name ---------------------------------------------------------------------------------


async def test_user_name_is_signed_and_keeps_the_token_out_of_the_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and path(request) == "me"
        assert_signed(request)
        assert request.url.params["fields"] == "name"
        return httpx.Response(200, json={"id": "1", "name": "Ana Pop"})

    assert await reports(handler).user_name(TOKEN) == "Ana Pop"


async def test_user_name_is_none_when_meta_refuses() -> None:
    recorder = Recorder(lambda _: graph_error(190, 400))
    assert await reports(recorder).user_name(TOKEN) is None
    assert len(recorder.requests) == 1


async def test_user_name_is_none_when_meta_is_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    assert await reports(handler).user_name(TOKEN) is None


async def test_user_name_is_none_without_a_name() -> None:
    assert await reports(lambda _: httpx.Response(200, json={"id": "1"})).user_name(TOKEN) is None


async def test_no_proof_without_an_app_secret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "appsecret_proof" not in request.url.params
        return httpx.Response(200, json={"name": "Ana"})

    client = MetaAdsReports(config(app_secret=""), transport=httpx.MockTransport(handler))
    assert await client.user_name(TOKEN) == "Ana"


# --- ad accounts and custom conversions --------------------------------------------------------


async def test_list_ad_accounts_follows_cursors_while_there_is_a_next_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert path(request) == "me/adaccounts"
        assert_signed(request)
        assert request.url.params["fields"] == "id,name,currency,account_status,timezone_name"
        assert request.url.params["limit"] == "100"
        if "after" not in request.url.params:
            return ok(
                [
                    {
                        "id": "act_1",
                        "name": "Alpha",
                        "currency": "RON",
                        "account_status": 1,
                        "timezone_name": "Europe/Bucharest",
                    }
                ],
                paging={"cursors": {"before": "b1", "after": "c1"}, "next": "https://next"},
            )
        assert request.url.params["after"] == "c1"
        # A cursor without `next` is the last page.
        return ok(
            [{"id": "act_2", "name": "Beta", "currency": "EUR", "account_status": 2}],
            paging={"cursors": {"before": "b2", "after": "c2"}},
        )

    recorder = Recorder(handler)
    accounts = await reports(recorder).list_ad_accounts(TOKEN)
    assert accounts == [
        AdAccount("act_1", "Alpha", "RON", 1, "Europe/Bucharest"),
        AdAccount("act_2", "Beta", "EUR", 2, ""),
    ]
    assert len(recorder.requests) == 2


async def test_list_ad_accounts_stops_on_an_empty_page() -> None:
    recorder = Recorder(lambda _: ok([], paging={"cursors": {"after": "c"}, "next": "n"}))
    assert await reports(recorder).list_ad_accounts(TOKEN) == []
    assert len(recorder.requests) == 1


async def test_list_ad_accounts_rejects_a_non_list_page() -> None:
    with pytest.raises(ReportError) as caught:
        await reports(lambda _: httpx.Response(200, json={"data": {}})).list_ad_accounts(TOKEN)
    assert caught.value.reason == "unavailable"


@pytest.mark.parametrize("token", ["", None])
async def test_missing_token_is_auth_failed_before_any_request(token: Any) -> None:
    recorder = Recorder(lambda _: pytest.fail("no request expected"))
    with pytest.raises(ReportAuthError):
        await reports(recorder).list_ad_accounts(token)
    assert recorder.requests == []


async def test_list_custom_conversions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert path(request) == f"{ACCOUNT}/customconversions"
        assert_signed(request)
        assert request.url.params["fields"] == (
            "id,name,description,custom_event_type,default_conversion_value"
        )
        return ok(
            [
                {
                    "id": "987",
                    "name": "Lead form",
                    "custom_event_type": "LEAD",
                    "description": "Thank-you page",
                    "default_conversion_value": 5,
                },
                {"id": 654},
            ]
        )

    conversions = await reports(handler).list_custom_conversions(TOKEN, ACCOUNT)
    assert conversions == [
        CustomConversion("987", "Lead form", "LEAD", "Thank-you page", 5),
        CustomConversion("654", "", "", "", None),
    ]
    assert conversions[0].action_type == "offsite_conversion.custom.987"


@pytest.mark.parametrize("account", ["123", "act_", "act_12a", "", None, 123])
async def test_invalid_ad_account_is_unavailable_without_a_request(account: Any) -> None:
    recorder = Recorder(lambda _: pytest.fail("no request expected"))
    with pytest.raises(ReportError) as caught:
        await reports(recorder).list_custom_conversions(TOKEN, account)
    assert caught.value.reason == "unavailable" and not caught.value.auth
    with pytest.raises(ReportError) as again:
        await reports(recorder).read_insights(
            TOKEN,
            account,
            fields=["spend"],
            level="ad",
            since=date(2026, 1, 1),
            until=date(2026, 1, 1),
        )
    assert again.value.reason == "unavailable"
    assert recorder.requests == []


# --- insights ----------------------------------------------------------------------------------


async def test_read_insights_sends_the_query_and_reads_the_days() -> None:
    rows = [
        {
            "campaign_id": "1",
            "spend": "10.50",
            "date_start": "2026-10-01",
            "date_stop": "2026-10-01",
        },
        {"campaign_id": "2", "spend": "3", "date_start": "2026-10-02", "date_stop": "2026-10-02"},
        {"campaign_id": "3", "spend": "1"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and path(request) == f"{ACCOUNT}/insights"
        assert_signed(request)
        params = request.url.params
        assert params["fields"] == "campaign_id,spend,actions"
        assert params["level"] == "campaign"
        assert params["limit"] == "500"
        assert json.loads(params["time_range"]) == {"since": "2026-10-01", "until": "2026-10-03"}
        assert params["time_range"] == '{"since":"2026-10-01","until":"2026-10-03"}'
        assert params["time_increment"] == "1"
        assert params["breakdowns"] == "age,gender"
        return ok(rows)

    report = await reports(handler).read_insights(
        TOKEN,
        ACCOUNT,
        fields=["campaign_id", "spend", "actions"],
        level="campaign",
        since=date(2026, 10, 1),
        until=date(2026, 10, 3),
        breakdowns=["age", "gender"],
    )
    assert report.records == rows
    assert report.days == [date(2026, 10, 1), date(2026, 10, 2), None]
    assert report.envelope == {}


async def test_read_insights_pages_and_omits_breakdowns() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "breakdowns" not in request.url.params
        if request.url.params.get("after") is None:
            return ok(
                [{"date_start": "2026-10-01"}],
                paging={"cursors": {"after": "x"}, "next": "https://next"},
            )
        assert request.url.params["after"] == "x"
        assert request.url.params["level"] == "ad"
        return ok([{"date_start": "2026-10-02"}], paging={"cursors": {"after": "y"}})

    report = await reports(handler).read_insights(
        TOKEN,
        ACCOUNT,
        fields=["spend"],
        level="ad",
        since=date(2026, 10, 1),
        until=date(2026, 10, 2),
    )
    assert report.days == [date(2026, 10, 1), date(2026, 10, 2)]


async def test_read_insights_without_a_day_column() -> None:
    report = await reports(lambda _: ok([{"date_start": "2026-10-01"}])).read_insights(
        TOKEN,
        ACCOUNT,
        fields=["spend"],
        level="account",
        since=date(2026, 10, 1),
        until=date(2026, 10, 31),
        time_increment=30,
        day_column=None,
    )
    assert report.days == [None]


# --- throttling and errors ---------------------------------------------------------------------


def flaky(first: httpx.Response, times: int = 1) -> Recorder:
    """`first` for the first `times` requests, then one page of data."""

    def handler(request: httpx.Request) -> httpx.Response:
        if len(recorder.requests) <= times:
            return first
        return ok([{"date_start": "2026-10-01"}])

    recorder = Recorder(handler)
    return recorder


async def read(recorder: Recorder, sleep: Sleeps) -> list[dict[str, Any]]:
    report = await reports(recorder, sleep).read_insights(
        TOKEN,
        ACCOUNT,
        fields=["spend"],
        level="ad",
        since=date(2026, 10, 1),
        until=date(2026, 10, 1),
    )
    return report.records


@pytest.mark.parametrize("code", [4, 17, 32, 341, 613, 80000, 80004, 80014])
async def test_graph_throttle_codes_are_retried(code: int) -> None:
    sleep = Sleeps()
    recorder = flaky(graph_error(code, 400))
    assert await read(recorder, sleep) == [{"date_start": "2026-10-01"}]
    assert len(recorder.requests) == 2
    assert sleep.calls == [2.0]


async def test_server_errors_are_retried_with_backoff() -> None:
    sleep = Sleeps()
    recorder = flaky(httpx.Response(503, text="down"), times=2)
    assert await read(recorder, sleep)
    assert sleep.calls == [2.0, 4.0]


async def test_network_errors_are_retried() -> None:
    sleep = Sleeps()
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return ok([])

    report = await reports(handler, sleep).read_insights(
        TOKEN,
        ACCOUNT,
        fields=["spend"],
        level="ad",
        since=date(2026, 10, 1),
        until=date(2026, 10, 1),
    )
    assert report.records == [] and sleep.calls == [2.0]


async def test_business_use_case_wait_is_honoured() -> None:
    sleep = Sleeps()
    recorder = flaky(graph_error(80000, 400, usage(1)))
    assert await read(recorder, sleep)
    assert sleep.calls == [60.0]


async def test_business_use_case_wait_takes_the_longest_entry() -> None:
    sleep = Sleeps()
    header = json.dumps(
        {
            "1": [{"estimated_time_to_regain_access": 0}],
            "2": [{"estimated_time_to_regain_access": 2}, {"estimated_time_to_regain_access": 1}],
        }
    )
    recorder = flaky(graph_error(80004, 400, {"x-business-use-case-usage": header}))
    assert await read(recorder, sleep)
    assert sleep.calls == [120.0]


async def test_a_zero_or_unreadable_wait_falls_back_to_backoff() -> None:
    sleep = Sleeps()
    recorder = flaky(graph_error(17, 400, usage(0)))
    assert await read(recorder, sleep)
    sleep2 = Sleeps()
    garbled = flaky(graph_error(17, 400, {"x-business-use-case-usage": "{not json"}))
    assert await read(garbled, sleep2)
    assert sleep.calls == sleep2.calls == [2.0]


async def test_a_long_throttle_fails_without_waiting() -> None:
    sleep = Sleeps()
    recorder = flaky(graph_error(80000, 400, usage(3)))
    with pytest.raises(ReportError) as caught:
        await read(recorder, sleep)
    assert caught.value.reason == "unavailable" and not caught.value.auth
    assert "3 minutes" in str(caught.value)
    assert sleep.calls == [] and len(recorder.requests) == 1


async def test_endless_throttling_gives_up_after_five_attempts() -> None:
    sleep = Sleeps()
    recorder = Recorder(lambda _: graph_error(17, 400))
    with pytest.raises(ReportError) as caught:
        await read(recorder, sleep)
    assert caught.value.reason == "unavailable" and not caught.value.auth
    assert len(recorder.requests) == 5
    assert sleep.calls == [2.0, 4.0, 8.0, 16.0]
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    "response",
    [
        graph_error(190, 400),
        graph_error(102, 400),
        graph_error(10, 400),
        graph_error(200, 403),
        graph_error(272, 400),
        httpx.Response(401, text="no"),
        httpx.Response(403, json={"error": {"message": "forbidden"}}),
    ],
)
async def test_auth_errors_fail_after_one_request(response: httpx.Response) -> None:
    sleep = Sleeps()
    recorder = Recorder(lambda _: response)
    with pytest.raises(ReportAuthError) as caught:
        await read(recorder, sleep)
    assert caught.value.reason == "auth_failed" and caught.value.auth
    assert len(recorder.requests) == 1 and sleep.calls == []
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    "response",
    [graph_error(100, 400), httpx.Response(404, text="gone"), httpx.Response(200, text="<html>")],
)
async def test_other_errors_are_unavailable_without_retrying(response: httpx.Response) -> None:
    sleep = Sleeps()
    recorder = Recorder(lambda _: response)
    with pytest.raises(ReportError) as caught:
        await read(recorder, sleep)
    assert caught.value.reason == "unavailable" and not caught.value.auth
    assert len(recorder.requests) == 1 and sleep.calls == []


@pytest.mark.parametrize("expires_in", ["soon", None, 0, ""])
async def test_an_unreadable_expiry_means_no_expiry(expires_in: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": TOKEN, "expires_in": expires_in})

    grant = await reports(handler).exchange_code("code")
    assert grant.credentials == {"access_token": TOKEN} and grant.expires_in is None


async def test_a_cursor_that_does_not_move_is_invalid_instead_of_paging_forever() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return ok([{"id": "act_1"}], paging={"cursors": {"after": "same"}, "next": "https://n"})

    recorder = Recorder(handler)
    with pytest.raises(ReportError) as caught:
        await reports(recorder).list_ad_accounts(TOKEN)
    assert caught.value.reason == "unavailable"
    assert len(recorder.requests) == 2
