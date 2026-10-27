from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import date
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from data_sources.config.schema import AuthConfig, ConnectorConfig
from data_sources.reports import Grant, ReportAuthError, ReportError, create_report_source
from data_sources.reports.http import Retry
from data_sources.reports.sources.tiktok_ads import (
    AUTH_CODES,
    RETRY_CODES,
    Advertiser,
    TikTokAdsReports,
    advertiser_ids,
    classify,
)

APP_ID = "app-id"
APP_SECRET = "app-secret"
REDIRECT = "https://app.example/api/v1/oauth/tiktok/callback"
TOKEN = "tiktok-token"
ACCOUNT = "7123456789012345678"
AUTHORIZED = [ACCOUNT]

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


def reports(handler: Handler, sleep: Sleeps | None = None) -> TikTokAdsReports:
    credentials = {"app_id": APP_ID, "app_secret": APP_SECRET, "redirect_uri": REDIRECT}
    config = ConnectorConfig(
        provider="tiktok_ads", auth=AuthConfig(type="oauth2", credentials=credentials)
    )
    source = create_report_source(
        config, transport=httpx.MockTransport(handler), sleep=sleep or Sleeps()
    )
    assert isinstance(source, TikTokAdsReports)
    return source


def success(data: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"code": 0, "message": "OK", "data": data})


def failure(code: int) -> httpx.Response:
    return httpx.Response(200, json={"code": code, "message": f"bad {TOKEN}", "data": {}})


def no_request(_: httpx.Request) -> httpx.Response:
    pytest.fail("no request expected")


def row(entity: str, day: str, **metrics: str) -> dict[str, Any]:
    return {
        "dimensions": {"campaign_id": entity, "stat_time_day": f"{day} 00:00:00"},
        "metrics": {"spend": "1.00", **metrics},
    }


# --- consent -----------------------------------------------------------------------------------


def test_authorize_url() -> None:
    url = reports(no_request).authorize_url("signed-state")
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
        "https://business-api.tiktok.com/portal/auth"
    )
    assert dict(parse_qsl(parts.query)) == {
        "app_id": APP_ID,
        "redirect_uri": REDIRECT,
        "state": "signed-state",
    }
    assert APP_SECRET not in url


async def test_exchange_code_posts_json_and_keeps_secrets_out_of_the_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert (
            str(request.url) == "https://business-api.tiktok.com/open_api/v1.3/oauth2/access_token/"
        )
        assert "Access-Token" not in request.headers
        assert json.loads(request.content) == {
            "app_id": APP_ID,
            "secret": APP_SECRET,
            "auth_code": "single-use-code",
            "return_advertiser_ids": True,
        }
        return success(
            {"access_token": TOKEN, "advertiser_ids": [int(ACCOUNT), "42", int(ACCOUNT)]}
        )

    recorder = Recorder(handler)
    grant = await reports(recorder).exchange_code("single-use-code")
    assert grant == Grant({"access_token": TOKEN, "advertiser_ids": [ACCOUNT, "42"]})
    assert TOKEN not in repr(grant)
    assert len(recorder.requests) == 1
    assert APP_SECRET not in str(recorder.requests[0].url)
    assert "single-use-code" not in str(recorder.requests[0].url)


@pytest.mark.parametrize("code", [40002, 40001, 40105])
async def test_a_refused_code_is_auth_failed(code: int) -> None:
    recorder = Recorder(lambda _: failure(code))
    with pytest.raises(ReportAuthError) as caught:
        await reports(recorder).exchange_code("bad")
    assert caught.value.reason == "auth_failed" and caught.value.auth
    assert TOKEN not in str(caught.value)
    assert len(recorder.requests) == 1


async def test_exchange_is_not_retried() -> None:
    sleep = Sleeps()
    recorder = Recorder(lambda _: failure(40100))
    with pytest.raises(ReportError) as caught:
        await reports(recorder, sleep).exchange_code("code")
    assert caught.value.reason == "unavailable" and not caught.value.auth
    assert len(recorder.requests) == 1 and sleep.calls == []


@pytest.mark.parametrize(
    "data",
    [
        {"advertiser_ids": [ACCOUNT]},
        {"access_token": "", "advertiser_ids": [ACCOUNT]},
        {"access_token": TOKEN, "advertiser_ids": "123"},
        {"access_token": TOKEN, "advertiser_ids": ["12a"]},
        {"access_token": TOKEN, "advertiser_ids": [True]},
    ],
)
async def test_exchange_without_a_token_or_a_valid_advertiser_list_is_auth_failed(
    data: dict[str, Any],
) -> None:
    with pytest.raises(ReportAuthError) as caught:
        await reports(lambda _: success(data)).exchange_code("code")
    assert caught.value.reason == "auth_failed"


async def test_exchange_without_ids_leaves_them_to_discovery() -> None:
    grant = await reports(lambda _: success({"access_token": TOKEN})).exchange_code("code")
    assert grant.credentials == {"access_token": TOKEN, "advertiser_ids": []}


# --- authorized advertiser discovery ------------------------------------------------------------


async def test_discovery_asks_for_the_tokens_advertisers_and_redacts_the_secret_from_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="httpx")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path.endswith("/oauth2/advertiser/get/")
        assert request.url.params["app_id"] == APP_ID
        assert request.url.params["secret"] == APP_SECRET
        assert request.headers["Access-Token"] == TOKEN
        return success({"list": [{"advertiser_id": ACCOUNT}, {"advertiser_id": int(ACCOUNT)}]})

    assert await reports(handler).authorized_advertiser_ids(TOKEN) == [ACCOUNT]
    assert "oauth2/advertiser/get/" in caplog.text and "redacted" in caplog.text
    assert APP_SECRET not in caplog.text and TOKEN not in caplog.text


@pytest.mark.parametrize(
    "items", [None, {}, [None], [{"advertiser_id": True}], [{"advertiser_id": "wrong"}]]
)
async def test_malformed_discovery_is_an_error_not_an_empty_list(items: Any) -> None:
    with pytest.raises(ReportError):
        await reports(lambda _: success({"list": items})).authorized_advertiser_ids(TOKEN)


@pytest.mark.parametrize(("code", "reason"), [(40105, "auth_failed"), (40002, "request_failed")])
async def test_discovery_errors_keep_their_reason(code: int, reason: str) -> None:
    with pytest.raises(ReportError) as caught:
        await reports(lambda _: failure(code)).authorized_advertiser_ids(TOKEN)
    assert caught.value.reason == reason and TOKEN not in str(caught.value)


async def test_list_accounts_discovers_the_advertisers_of_a_grant_without_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/advertiser/get/"):
            return success({"list": [{"advertiser_id": ACCOUNT}]})
        assert json.loads(request.url.params["advertiser_ids"]) == [ACCOUNT]
        return success({"list": [{"advertiser_id": ACCOUNT, "name": "Store"}]})

    accounts = await reports(handler).list_accounts({"access_token": TOKEN, "advertiser_ids": []})
    assert [a.id for a in accounts] == [ACCOUNT]


async def test_exchange_with_no_data_is_unavailable() -> None:
    response = httpx.Response(200, json={"code": 0, "message": "OK"})
    with pytest.raises(ReportError) as caught:
        await reports(lambda _: response).exchange_code("code")
    assert caught.value.reason == "unavailable"


# --- advertiser ids ----------------------------------------------------------------------------


def test_advertiser_ids_are_strings_deduplicated_in_order() -> None:
    assert advertiser_ids([7, "3", 7, 12345678901234567890]) == ["7", "3", "12345678901234567890"]
    assert advertiser_ids([]) == []


@pytest.mark.parametrize(
    "values",
    [None, "123", {"id": "1"}, [""], ["-1"], [-1], ["1.5"], [1.0], [True], [None], ["١٢٣"]],
)
def test_invalid_advertiser_ids_are_auth_failed(values: Any) -> None:
    with pytest.raises(ReportAuthError) as caught:
        advertiser_ids(values)
    assert caught.value.reason == "auth_failed"


# --- advertisers -------------------------------------------------------------------------------


async def test_list_advertisers_asks_in_batches_of_ten() -> None:
    ids = [str(n) for n in range(1, 24)]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/open_api/v1.3/advertiser/info/"
        assert request.headers["Access-Token"] == TOKEN
        assert TOKEN not in str(request.url) and APP_SECRET not in str(request.url)
        assert json.loads(request.url.params["fields"]) == [
            "advertiser_id",
            "name",
            "currency",
            "timezone",
        ]
        wanted = json.loads(request.url.params["advertiser_ids"])
        # Answered out of order, one missing, ids as numbers: the result follows the grant.
        items = [
            {"advertiser_id": int(a), "name": f"Shop {a}", "currency": "RON", "timezone": "UTC"}
            for a in reversed(wanted)
            if a != "5"
        ]
        return success({"list": items})

    recorder = Recorder(handler)
    advertisers = await reports(recorder).list_advertisers(TOKEN, ids)
    batches = [json.loads(r.url.params["advertiser_ids"]) for r in recorder.requests]
    assert batches == [ids[:10], ids[10:20], ids[20:]]
    assert [a.account for a in advertisers] == [a for a in ids if a != "5"]
    assert advertisers[0] == Advertiser("1", "Shop 1", "RON", "UTC")


async def test_list_advertisers_falls_back_on_names() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return success(
            {
                "list": [
                    {"advertiser_id": ACCOUNT, "advertiser_name": "Legacy"},
                    {"advertiser_id": "9"},
                ]
            }
        )

    advertisers = await reports(handler).list_advertisers(TOKEN, [ACCOUNT, "9"])
    assert advertisers == [Advertiser(ACCOUNT, "Legacy", "", ""), Advertiser("9", "9", "", "")]


async def test_no_authorized_advertisers_means_no_request() -> None:
    assert await reports(no_request).list_advertisers(TOKEN, []) == []


async def test_an_unexpected_advertiser_is_unavailable() -> None:
    response = success({"list": [{"advertiser_id": "999", "name": "Someone else"}]})
    with pytest.raises(ReportError) as caught:
        await reports(lambda _: response).list_advertisers(TOKEN, AUTHORIZED)
    assert caught.value.reason == "unavailable" and not caught.value.auth


@pytest.mark.parametrize("data", [{"list": None}, {}, {"list": ["x"]}])
async def test_an_invalid_advertiser_list_is_unavailable(data: dict[str, Any]) -> None:
    with pytest.raises(ReportError) as caught:
        await reports(lambda _: success(data)).list_advertisers(TOKEN, AUTHORIZED)
    assert caught.value.reason == "unavailable"


async def test_list_advertisers_validates_the_grant_and_token_first() -> None:
    with pytest.raises(ReportAuthError):
        await reports(no_request).list_advertisers(TOKEN, ["abc"])
    with pytest.raises(ReportAuthError):
        await reports(no_request).list_advertisers("", AUTHORIZED)


# --- reports -----------------------------------------------------------------------------------


async def read(
    handler: Handler,
    since: date = date(2026, 10, 1),
    until: date = date(2026, 10, 1),
    *,
    dimensions: list[str] | None = None,
    data_level: str = "AUCTION_CAMPAIGN",
    sleep: Sleeps | None = None,
) -> Any:
    return await reports(handler, sleep).read_report(
        TOKEN,
        AUTHORIZED,
        ACCOUNT,
        data_level=data_level,
        dimensions=dimensions or ["campaign_id", "stat_time_day"],
        metrics=["spend", "impressions"],
        since=since,
        until=until,
    )


async def test_read_report_windows_pages_and_keeps_raw_rows() -> None:
    seen: list[dict[str, str]] = []
    rows: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/open_api/v1.3/report/integrated/get/"
        assert request.headers["Access-Token"] == TOKEN
        assert TOKEN not in str(request.url)
        p = dict(request.url.params)
        seen.append(p)
        assert p["advertiser_id"] == ACCOUNT
        assert p["report_type"] == "BASIC" and p["service_type"] == "AUCTION"
        assert p["data_level"] == "AUCTION_CAMPAIGN"
        assert json.loads(p["dimensions"]) == ["campaign_id", "stat_time_day"]
        assert json.loads(p["metrics"]) == ["spend", "impressions"]
        assert json.loads(p["filtering"]) == [
            {"field_name": "campaign_status", "filter_type": "IN", "filter_value": '["STATUS_ALL"]'}
        ]
        assert p["page_size"] == "1000"
        item = row(f"c-{p['page']}", p["start_date"])
        rows.append(item)
        return success({"list": [item], "page_info": {"page": int(p["page"]), "total_page": 2}})

    report = await read(handler, date(2026, 9, 1), date(2026, 10, 15))
    assert [(p["start_date"], p["end_date"], p["page"]) for p in seen] == [
        ("2026-09-01", "2026-09-30", "1"),
        ("2026-09-01", "2026-09-30", "2"),
        ("2026-10-01", "2026-10-15", "1"),
        ("2026-10-01", "2026-10-15", "2"),
    ]
    assert report.records == rows
    assert report.days == [date(2026, 9, 1)] * 2 + [date(2026, 10, 1)] * 2
    assert report.envelope == {"advertiser_id": ACCOUNT}


@pytest.mark.parametrize(
    ("entity", "status"),
    [("campaign_id", "campaign_status"), ("adgroup_id", "adgroup_status"), ("ad_id", "ad_status")],
)
async def test_every_entity_status_is_asked_for(entity: str, status: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        # The entity id need not come first.
        assert json.loads(params["dimensions"]) == ["stat_time_day", entity]
        assert json.loads(params["filtering"])[0]["field_name"] == status
        assert json.loads(json.loads(params["filtering"])[0]["filter_value"]) == ["STATUS_ALL"]
        item = {"dimensions": {entity: "1", "stat_time_day": "2026-10-01"}, "metrics": {}}
        return success({"list": [item], "page_info": {"total_page": 1}})

    report = await read(handler, dimensions=["stat_time_day", entity])
    assert report.days == [date(2026, 10, 1)]


@pytest.mark.parametrize("data", [{"list": [], "page_info": {"total_page": 0}}, {"list": []}])
async def test_no_rows_is_a_successful_report(data: dict[str, Any]) -> None:
    report = await read(lambda _: success(data))
    assert report.records == report.days == []
    assert report.envelope == {"advertiser_id": ACCOUNT}


async def test_an_unauthorized_advertiser_never_reaches_tiktok() -> None:
    with pytest.raises(ReportAuthError) as caught:
        await reports(no_request).read_report(
            TOKEN,
            AUTHORIZED,
            "999",
            data_level="AUCTION_CAMPAIGN",
            dimensions=["campaign_id", "stat_time_day"],
            metrics=["spend"],
            since=date(2026, 10, 1),
            until=date(2026, 10, 1),
        )
    assert caught.value.reason == "auth_failed" and caught.value.auth


@pytest.mark.parametrize(
    "item",
    [
        {},
        "row",
        {"dimensions": {"campaign_id": "1", "stat_time_day": "2026-10-01"}},
        {"dimensions": {"campaign_id": "1", "stat_time_day": "2026-10-01"}, "metrics": []},
        {"dimensions": {"stat_time_day": "2026-10-01"}, "metrics": {}},
        {"dimensions": {"campaign_id": "", "stat_time_day": "2026-10-01"}, "metrics": {}},
        {"dimensions": {"campaign_id": "1", "stat_time_day": "2025-01-01"}, "metrics": {}},
        {"dimensions": {"campaign_id": "1", "stat_time_day": "2026-10-02"}, "metrics": {}},
        {"dimensions": {"campaign_id": "1", "stat_time_day": "soon"}, "metrics": {}},
        {"dimensions": {"campaign_id": "1"}, "metrics": {}},
        {"dimensions": "campaign_id", "metrics": {}},
    ],
)
async def test_invalid_rows_fail_the_report(item: Any) -> None:
    response = success({"list": [item], "page_info": {"total_page": 1}})
    with pytest.raises(ReportError) as caught:
        await read(lambda _: response)
    assert caught.value.reason == "unavailable" and not caught.value.auth


@pytest.mark.parametrize(
    "data",
    [
        {"list": None, "page_info": {"total_page": 1}},
        {"list": [row("1", "2026-10-01")]},
        {"list": [row("1", "2026-10-01")], "page_info": {"total_page": "1"}},
        {"list": [row("1", "2026-10-01")], "page_info": {"total_page": True}},
        {"list": [row("1", "2026-10-01")], "page_info": {"total_page": -1}},
        {"list": [row("1", "2026-10-01")], "page_info": {"total_page": 0}},
        {"list": [row("1", "2026-10-01")], "page_info": {"total_page": 1, "page": 2}},
        {"list": [], "page_info": {"total_page": 3}},
        {"list": [row("1", "2026-10-01")], "page_info": "1"},
    ],
)
async def test_invalid_pagination_fails_the_report(data: dict[str, Any]) -> None:
    with pytest.raises(ReportError) as caught:
        await read(lambda _: success(data))
    assert caught.value.reason == "unavailable"


async def test_a_failed_later_page_fails_the_whole_report() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["page"] == "2":
            return failure(40105)
        return success({"list": [row("1", "2026-10-01")], "page_info": {"total_page": 2}})

    with pytest.raises(ReportAuthError) as caught:
        await read(handler)
    assert caught.value.reason == "auth_failed"
    assert TOKEN not in str(caught.value)


# --- classify and retries ----------------------------------------------------------------------


def respond(status: int, body: Any = None, text: str | None = None) -> httpx.Response:
    if text is not None:
        return httpx.Response(status, text=text)
    return httpx.Response(status, json=body)


def test_classify_reads_errors_inside_http_200() -> None:
    assert classify(respond(200, {"code": 0, "data": {}})) is None
    for code in RETRY_CODES:
        assert isinstance(classify(respond(200, {"code": code})), Retry)
    for code in AUTH_CODES:
        verdict = classify(respond(200, {"code": code}))
        assert not isinstance(verdict, Retry) and verdict is not None
        assert verdict.reason == "auth_failed"
    refused = classify(respond(200, {"code": 40002}))
    assert not isinstance(refused, Retry) and refused is not None
    assert refused.reason == "request_failed"


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (respond(200, text="<html>"), "unavailable"),
        (respond(200, ["code", 0]), "unavailable"),
        (respond(200, {"code": "0"}), "unavailable"),
        (respond(200, {"code": False}), "unavailable"),
        (respond(200, {"message": "no code"}), "unavailable"),
        (respond(401, text="no"), "auth_failed"),
        (respond(403, {"code": 0}), "auth_failed"),
        (respond(404, text="not found"), "unavailable"),
        (respond(404, {"code": 0}), "request_failed"),
        (respond(400, {"code": 40002}), "request_failed"),
    ],
)
def test_classify_failures(response: httpx.Response, reason: str) -> None:
    verdict = classify(response)
    assert verdict is not None and not isinstance(verdict, Retry)
    assert verdict.reason == reason


@pytest.mark.parametrize("response", [respond(429, text=""), respond(500, text=""), respond(503)])
def test_classify_retries_rate_limits_and_server_errors(response: httpx.Response) -> None:
    verdict = classify(response)
    assert isinstance(verdict, Retry) and verdict.problem.reason == "unavailable"


async def test_gets_are_retried_with_exponential_backoff() -> None:
    sleep = Sleeps()
    answers = [respond(503, text="down"), failure(40100)]

    def handler(_: httpx.Request) -> httpx.Response:
        return answers.pop(0) if answers else success({"list": []})

    recorder = Recorder(handler)
    assert await reports(recorder, sleep).list_advertisers(TOKEN, AUTHORIZED) == []
    assert len(recorder.requests) == 3 and sleep.calls == [0.5, 1.0]


async def test_endless_rate_limiting_gives_up_after_three_attempts() -> None:
    sleep = Sleeps()
    recorder = Recorder(lambda _: failure(40016))
    with pytest.raises(ReportError) as caught:
        await read(recorder, sleep=sleep)
    assert caught.value.reason == "unavailable" and not caught.value.auth
    assert len(recorder.requests) == 3 and sleep.calls == [0.5, 1.0]


async def test_network_errors_are_retried_then_unavailable() -> None:
    sleep = Sleeps()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed {request.url}", request=request)

    with pytest.raises(ReportError) as caught:
        await reports(handler, sleep).list_advertisers(TOKEN, AUTHORIZED)
    assert caught.value.reason == "unavailable"
    assert sleep.calls == [0.5, 1.0]


@pytest.mark.parametrize("code", sorted(AUTH_CODES))
async def test_auth_codes_fail_at_once(code: int) -> None:
    sleep = Sleeps()
    recorder = Recorder(lambda _: failure(code))
    with pytest.raises(ReportAuthError) as caught:
        await reports(recorder, sleep).list_advertisers(TOKEN, AUTHORIZED)
    assert caught.value.reason == "auth_failed" and caught.value.auth
    assert len(recorder.requests) == 1 and sleep.calls == []
    assert TOKEN not in str(caught.value)


async def test_other_codes_are_request_failed_without_retrying() -> None:
    sleep = Sleeps()
    recorder = Recorder(lambda _: failure(40002))
    with pytest.raises(ReportError) as caught:
        await read(recorder, sleep=sleep)
    assert caught.value.reason == "request_failed" and not caught.value.auth
    assert len(recorder.requests) == 1 and sleep.calls == []


async def test_non_json_answers_are_unavailable() -> None:
    recorder = Recorder(lambda _: respond(200, text="<html>maintenance</html>"))
    with pytest.raises(ReportError) as caught:
        await read(recorder)
    assert caught.value.reason == "unavailable"
    assert len(recorder.requests) == 1
