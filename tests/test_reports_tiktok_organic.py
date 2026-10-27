"""TikTok organic (Display API) against `httpx.MockTransport`."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from data_sources.config.schema import AuthConfig, ConnectorConfig
from data_sources.reports import ReportAuthError, ReportError, create_report_source
from data_sources.reports.http import Retry
from data_sources.reports.sources import tiktok_organic as organic
from data_sources.reports.sources.tiktok_organic import SCOPES, TikTokOrganicReports, classify

KEY = "client-key"
SECRET = "client-secret"
REDIRECT = "https://app.example/api/v1/oauth/tiktok-organic/callback"
OPEN_ID = "open-id-1"
TODAY = date(2026, 10, 27)
ALL_SCOPES = ",".join(SCOPES)
CREDENTIALS = {
    "access_token": "access-1",
    "refresh_token": "refresh-1",
    "open_id": OPEN_ID,
    "scope": ALL_SCOPES,
}

Handler = Callable[[httpx.Request], httpx.Response]


async def no_wait(_: float) -> None:
    pass


def reports(handler: Handler) -> TikTokOrganicReports:
    config = ConnectorConfig(
        provider="tiktok_organic",
        auth=AuthConfig(
            type="oauth2",
            credentials={"client_key": KEY, "client_secret": SECRET, "redirect_uri": REDIRECT},
        ),
    )
    source = create_report_source(config, transport=httpx.MockTransport(handler), sleep=no_wait)
    assert isinstance(source, TikTokOrganicReports)
    return source


def ok(data: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"data": data, "error": {"code": "ok", "message": ""}})


def refused(code: str, status: int = 400, message: str = "no") -> httpx.Response:
    error = {"code": code, "message": message, "log_id": "log-9"}
    return httpx.Response(status, json={"data": {}, "error": error})


def token_body(**over: Any) -> dict[str, Any]:
    return {
        "access_token": "access-2",
        "refresh_token": "refresh-2",
        "open_id": OPEN_ID,
        "scope": ALL_SCOPES,
        "expires_in": 86400,
        "refresh_expires_in": 31536000,
        **over,
    }


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(organic, "_today", lambda: TODAY)


def test_authorize_url_asks_for_every_scope_with_the_client_key() -> None:
    url = urlsplit(reports(lambda _: pytest.fail("no request")).authorize_url("state-1"))
    query = dict(parse_qsl(url.query))
    assert (url.netloc, url.path) == ("www.tiktok.com", "/v2/auth/authorize/")
    assert query == {
        "client_key": KEY,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": ALL_SCOPES,
        "state": "state-1",
    }
    assert SECRET not in url.query


async def test_exchange_code_posts_the_secret_in_the_body_and_keeps_the_tokens() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=token_body())

    grant = await reports(handler).exchange_code("the-code")
    (request,) = seen
    assert request.method == "POST" and request.url.path == "/v2/oauth/token/"
    assert SECRET not in str(request.url) and "the-code" not in str(request.url)
    assert dict(parse_qsl(request.content.decode())) == {
        "client_key": KEY,
        "client_secret": SECRET,
        "grant_type": "authorization_code",
        "code": "the-code",
        "redirect_uri": REDIRECT,
    }
    assert grant.credentials == {
        "access_token": "access-2",
        "refresh_token": "refresh-2",
        "open_id": OPEN_ID,
        "scope": ",".join(sorted(SCOPES)),
    }
    assert grant.expires_in == 31536000


async def test_a_consent_that_left_permissions_out_is_access_denied() -> None:
    handler = lambda _: httpx.Response(200, json=token_body(scope="user.info.basic,video.list"))  # noqa: E731
    with pytest.raises(ReportError) as caught:
        await reports(handler).exchange_code("c")
    assert caught.value.reason == "access_denied" and "user.info.stats" in caught.value.message


@pytest.mark.parametrize("status", [400, 401])
async def test_a_refused_code_is_auth_failed_with_the_reason_apart(status: int) -> None:
    body = {"error": "invalid_grant", "error_description": "Authorization code is expired."}
    with pytest.raises(ReportAuthError) as caught:
        await reports(lambda _: httpx.Response(status, json=body)).exchange_code("c")
    assert caught.value.reason == "auth_failed"
    assert "expired" not in caught.value.message and "expired" in caught.value.diagnostics


async def test_refresh_keeps_who_and_what_and_returns_the_rotated_refresh_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        form = dict(parse_qsl(request.content.decode()))
        assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "refresh-1"
        return httpx.Response(200, json={k: v for k, v in token_body().items() if k != "open_id"})

    grant = await reports(handler).refresh_grant(CREDENTIALS)
    assert grant.credentials["access_token"] == "access-2"
    assert grant.credentials["refresh_token"] == "refresh-2"  # the one to keep
    assert grant.credentials["open_id"] == OPEN_ID


async def test_a_refresh_token_that_no_longer_works_is_auth_failed() -> None:
    handler = lambda _: httpx.Response(400, json={"error": "invalid_grant"})  # noqa: E731
    with pytest.raises(ReportAuthError):
        await reports(handler).refresh_grant(CREDENTIALS)
    with pytest.raises(ReportAuthError):
        await reports(handler).refresh_grant({"access_token": "x"})


async def test_the_one_account_is_the_user() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/user/info/"
        assert request.headers["Authorization"] == "Bearer access-1"
        assert "access-1" not in str(request.url)
        assert set(request.url.params["fields"].split(",")) == set(organic.USER_FIELDS)
        return ok({"user": {"open_id": OPEN_ID, "display_name": "Ionut", "username": "ionut"}})

    (account,) = await reports(handler).list_accounts(CREDENTIALS)
    assert (account.id, account.name) == (OPEN_ID, "Ionut")


def video(n: int) -> dict[str, Any]:
    return {"id": f"v{n}", "view_count": n * 10, "like_count": n}


async def test_the_account_report_is_a_snapshot_labelled_with_today() -> None:
    user = {"open_id": OPEN_ID, "follower_count": 12, "video_count": 3}
    report = await reports(lambda _: ok({"user": user})).read(
        CREDENTIALS, OPEN_ID, {"report": "account"}, date(2026, 10, 1), date(2026, 10, 26)
    )
    assert report.records == [user] and report.days == [TODAY]
    assert report.envelope == {"open_id": OPEN_ID, "report": "account"}


async def test_videos_are_paged_by_cursor_until_there_are_no_more() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/v2/video/list/"
        assert set(request.url.params["fields"].split(",")) == set(organic.VIDEO_FIELDS)
        body = json.loads(request.content)
        seen.append(body)
        if "cursor" not in body:
            return ok({"videos": [video(1), video(2)], "has_more": True, "cursor": 1700})
        return ok({"videos": [video(3)], "has_more": False, "cursor": 1600})

    report = await reports(handler).read(CREDENTIALS, OPEN_ID, {"report": "videos"}, TODAY, TODAY)
    assert seen == [{"max_count": 20}, {"max_count": 20, "cursor": 1700}]
    assert [v["id"] for v in report.records] == ["v1", "v2", "v3"]
    assert report.days == [TODAY] * 3


async def test_no_videos_is_an_empty_report() -> None:
    report = await reports(lambda _: ok({"has_more": False, "cursor": 0})).read(
        CREDENTIALS, OPEN_ID, {"report": "videos"}, TODAY, TODAY
    )
    assert report.records == [] and report.days == []


async def test_a_cursor_that_does_not_move_fails_instead_of_paging_forever() -> None:
    handler = lambda _: ok({"videos": [video(1)], "has_more": True, "cursor": 5})  # noqa: E731
    with pytest.raises(ReportError) as caught:
        await reports(handler).read(CREDENTIALS, OPEN_ID, {"report": "videos"}, TODAY, TODAY)
    assert caught.value.reason == "unavailable"


@pytest.mark.parametrize("videos", [[{"title": "no id"}], "x", [1]])
async def test_invalid_videos_fail_the_report(videos: Any) -> None:
    with pytest.raises(ReportError):
        await reports(lambda _: ok({"videos": videos, "has_more": False})).read(
            CREDENTIALS, OPEN_ID, {"report": "videos"}, TODAY, TODAY
        )


async def test_another_users_account_and_unknown_reports_never_reach_tiktok() -> None:
    source = reports(lambda _: pytest.fail("no request expected"))
    with pytest.raises(ReportAuthError):
        await source.read(CREDENTIALS, "someone-else", {"report": "account"}, TODAY, TODAY)
    with pytest.raises(ReportError) as caught:
        await source.read(CREDENTIALS, OPEN_ID, {"report": "dms"}, TODAY, TODAY)
    assert caught.value.reason == "request_failed"


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (refused("access_token_invalid", 401), "auth_failed"),
        (refused("scope_not_authorized", 403), "auth_failed"),
        (refused("anything", 401), "auth_failed"),
        (refused("invalid_params", 400, "bad fields"), "request_failed"),
    ],
)
async def test_failures_are_classified(response: httpx.Response, reason: str) -> None:
    with pytest.raises(ReportError) as caught:
        await reports(lambda _: response).read(
            CREDENTIALS, OPEN_ID, {"report": "account"}, TODAY, TODAY
        )
    assert caught.value.reason == reason


async def test_a_refused_request_says_why_in_the_diagnostics_only() -> None:
    with pytest.raises(ReportError) as caught:
        await reports(lambda _: refused("invalid_params", 400, "bad fields")).read(
            CREDENTIALS, OPEN_ID, {"report": "account"}, TODAY, TODAY
        )
    assert "bad fields" not in str(caught.value)
    assert caught.value.diagnostics == "invalid_params: bad fields (log log-9)"


def test_rate_limits_and_server_errors_are_retried() -> None:
    for response in (
        httpx.Response(429, json={}),
        httpx.Response(503, json={}),
        refused("rate_limit_exceeded", 200),
    ):
        assert isinstance(classify(response), Retry)
    assert classify(ok({})) is None


async def test_a_network_failure_is_unavailable_without_the_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(ReportError) as caught:
        await reports(handler).read(CREDENTIALS, OPEN_ID, {"report": "account"}, TODAY, TODAY)
    assert caught.value.reason == "unavailable" and "access-1" not in str(caught.value)
