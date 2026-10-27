"""The shared pieces of `data_sources.reports`: the HTTP client and its retries, errors, paging,
date windows and Google's OAuth steps."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import date
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from data_sources.core.exceptions import AuthenticationError
from data_sources.reports import Problem, ReportAuthError, ReportError, parse_day
from data_sources.reports.http import (
    ApiClient,
    Retry,
    Verdict,
    exponential,
    http_status,
    linear,
)
from data_sources.reports.oauth import (
    GOOGLE_AUTH_URL,
    GOOGLE_TOKEN_URL,
    GoogleGrant,
    GoogleOAuth,
    consent_url,
    email_from_id_token,
)
from data_sources.reports.paging import link_pages, token_pages
from data_sources.reports.windows import each_day, windows

URL = "https://api.example.com/v1/things"
UNAVAILABLE = Problem("unavailable", "Example is unavailable.")
AUTH_FAILED = Problem("auth_failed", "Authorize again.")
FAILED = Problem("request_failed", "Example refused the request.")
UNREACHABLE = Problem("unavailable", "Example could not be reached.")
INVALID = Problem("unavailable", "Example returned an invalid response.")
SCOPE = "https://www.googleapis.com/auth/analytics.readonly"


class Sleeper:
    """A `sleep` that records how long it was asked to wait, and doesn't."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


Handler = Callable[[httpx.Request], httpx.Response]


def replay(*responses: httpx.Response) -> tuple[Handler, list[httpx.Request]]:
    """A handler answering with `responses` in turn, and the requests it saw."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses[min(len(seen), len(responses)) - 1]

    return handler, seen


def api(
    handler: Handler,
    sleep: Sleeper,
    classify: Callable[[httpx.Response], Verdict] | None = None,
    **kwargs: Any,
) -> ApiClient:
    return ApiClient(
        classify or http_status(unavailable=UNAVAILABLE, auth_failed=AUTH_FAILED, failed=FAILED),
        unreachable=UNREACHABLE,
        invalid=INVALID,
        transport=httpx.MockTransport(handler),
        sleep=sleep,
        **kwargs,
    )


# --- errors ---------------------------------------------------------------------------------


def test_problem_error_picks_the_auth_class_only_for_auth_failed() -> None:
    error = Problem("auth_failed", "Authorize again.").error()
    assert type(error) is ReportAuthError
    assert isinstance(error, AuthenticationError)
    assert error.auth and error.reason == "auth_failed" and error.message == "Authorize again."
    for reason in ("unavailable", "request_failed", "access_denied", "customer_not_enabled"):
        other = Problem(reason, "x").error()
        assert type(other) is ReportError
        assert not other.auth and other.reason == reason


def test_report_error_message_defaults_to_the_reason() -> None:
    assert str(ReportError("unavailable")) == "unavailable"
    assert str(ReportError("unavailable", "Try later.")) == "Try later."


# --- ApiClient ------------------------------------------------------------------------------


async def test_get_is_retried_with_exponential_backoff_until_accepted() -> None:
    handler, seen = replay(
        httpx.Response(503), httpx.Response(429), httpx.Response(200, json={"ok": True})
    )
    sleep = Sleeper()
    async with api(handler, sleep) as client:
        assert await client.json("GET", URL) == {"ok": True}
    assert len(seen) == 3
    assert sleep.waits == [0.5, 1.0]


async def test_retries_run_out_and_raise_the_retry_problem() -> None:
    handler, seen = replay(httpx.Response(500))
    sleep = Sleeper()
    async with api(handler, sleep, attempts=4, backoff=linear(2.0)) as client:
        with pytest.raises(ReportError) as caught:
            await client.request("GET", URL)
    assert caught.value.reason == "unavailable" and caught.value.message == UNAVAILABLE.message
    assert len(seen) == 4
    assert sleep.waits == [2.0, 4.0, 6.0]


async def test_retry_after_is_honoured_over_the_backoff() -> None:
    def classify(response: httpx.Response) -> Verdict:
        if response.status_code == 429:
            return Retry(UNAVAILABLE, float(response.headers["Retry-After"]))
        return None

    handler, _ = replay(
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json={}),
    )
    sleep = Sleeper()
    async with api(handler, sleep, classify) as client:
        await client.request("GET", URL)
    assert sleep.waits == [7.0, 0.0]


async def test_post_is_not_retried_unless_asked() -> None:
    handler, seen = replay(httpx.Response(503), httpx.Response(200, json={}))
    sleep = Sleeper()
    async with api(handler, sleep) as client:
        with pytest.raises(ReportError):
            await client.request("POST", URL, json={})
    assert len(seen) == 1 and sleep.waits == []

    handler, seen = replay(httpx.Response(503), httpx.Response(200, json={"n": 1}))
    async with api(handler, sleep) as client:
        assert await client.json("POST", URL, retry=True, json={}) == {"n": 1}
    assert len(seen) == 2 and sleep.waits == [0.5]


async def test_get_is_not_retried_when_retry_is_false() -> None:
    handler, seen = replay(httpx.Response(503), httpx.Response(200))
    async with api(handler, Sleeper()) as client:
        with pytest.raises(ReportError):
            await client.request("GET", URL, retry=False)
    assert len(seen) == 1


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_problems_raise_auth_errors_without_retrying(status: int) -> None:
    handler, seen = replay(httpx.Response(status, json={"error": "nope"}))
    sleep = Sleeper()
    async with api(handler, sleep) as client:
        with pytest.raises(ReportAuthError) as caught:
            await client.request("GET", URL)
    assert caught.value.auth and caught.value.reason == "auth_failed"
    assert len(seen) == 1 and sleep.waits == []


async def test_other_client_errors_fail_at_once() -> None:
    handler, seen = replay(httpx.Response(404, text="missing"))
    async with api(handler, Sleeper()) as client:
        with pytest.raises(ReportError) as caught:
            await client.request("GET", URL)
    assert caught.value.reason == "request_failed" and not caught.value.auth
    assert len(seen) == 1


async def test_unreachable_hides_the_url_and_its_token() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise httpx.ConnectError(f"cannot connect to {request.url}", request=request)

    sleep = Sleeper()
    async with api(handler, sleep) as client:
        with pytest.raises(ReportError) as caught:
            await client.request("GET", URL, params={"access_token": "secret-token"})
    error = caught.value
    assert error.reason == "unavailable" and error.message == UNREACHABLE.message
    assert "secret-token" not in str(error) and "api.example.com" not in str(error)
    assert error.__cause__ is None and error.__suppress_context__
    assert len(calls) == 3 and sleep.waits == [0.5, 1.0]
    assert "secret-token" in calls[0]


async def test_a_network_error_then_success_is_retried() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"ok": 1})

    async with api(handler, Sleeper()) as client:
        assert await client.json("GET", URL) == {"ok": 1}


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json=[1, 2]),
        httpx.Response(200, json="text"),
        httpx.Response(200, text="<html>"),
        httpx.Response(200),
    ],
)
async def test_json_that_is_not_an_object_is_invalid(response: httpx.Response) -> None:
    handler, seen = replay(response)
    async with api(handler, Sleeper()) as client:
        with pytest.raises(ReportError) as caught:
            await client.json("GET", URL)
    assert caught.value.reason == "unavailable" and caught.value.message == INVALID.message
    assert caught.value.__cause__ is None
    assert len(seen) == 1


async def test_client_sends_base_url_and_headers() -> None:
    handler, seen = replay(httpx.Response(200, json={}))
    async with api(
        handler, Sleeper(), base_url="https://api.example.com", headers={"X-Key": "k"}
    ) as client:
        await client.json("GET", "/v1/things", headers={"X-Extra": "e"})
    assert str(seen[0].url) == URL
    assert seen[0].headers["X-Key"] == "k" and seen[0].headers["X-Extra"] == "e"


def test_http_status_rules() -> None:
    classify = http_status(unavailable=UNAVAILABLE, auth_failed=AUTH_FAILED, failed=FAILED)
    request = httpx.Request("GET", URL)

    def verdict(status: int) -> Verdict:
        return classify(httpx.Response(status, request=request))

    assert verdict(200) is None and verdict(204) is None and verdict(302) is None
    assert verdict(429) == Retry(UNAVAILABLE) and verdict(502) == Retry(UNAVAILABLE)
    assert verdict(401) is AUTH_FAILED and verdict(403) is AUTH_FAILED
    assert verdict(400) is FAILED and verdict(404) is FAILED

    only_401 = http_status(
        unavailable=UNAVAILABLE,
        auth_failed=AUTH_FAILED,
        failed=FAILED,
        auth_statuses=frozenset({401}),
    )
    assert only_401(httpx.Response(403, request=request)) is FAILED


def test_backoff_shapes() -> None:
    assert [exponential()(n) for n in range(4)] == [0.5, 1.0, 2.0, 4.0]
    assert [exponential(1.0)(n) for n in range(3)] == [1.0, 2.0, 4.0]
    assert [linear()(n) for n in range(3)] == [1.0, 2.0, 3.0]


# --- paging ---------------------------------------------------------------------------------


async def test_token_pages_follow_until_no_token() -> None:
    pages = {None: {"n": 1, "nextPageToken": "a"}, "a": {"n": 2, "nextPageToken": "b"}}
    pages["b"] = {"n": 3, "nextPageToken": ""}
    asked: list[str | None] = []

    async def fetch(token: str | None) -> dict[str, Any]:
        asked.append(token)
        return pages[token]

    bodies = [body async for body in token_pages(fetch, invalid=INVALID)]
    assert [b["n"] for b in bodies] == [1, 2, 3]
    assert asked == [None, "a", "b"]


async def test_token_pages_use_the_given_key() -> None:
    async def fetch(token: str | None) -> dict[str, Any]:
        return {"cursor": "next"} if token is None else {}

    bodies = [b async for b in token_pages(fetch, invalid=INVALID, token_key="cursor")]
    assert len(bodies) == 2


@pytest.mark.parametrize("token", ["loop", 5])
async def test_token_pages_refuse_a_repeated_or_bad_token(token: object) -> None:
    asked: list[str | None] = []

    async def fetch(page: str | None) -> dict[str, Any]:
        asked.append(page)
        return {"nextPageToken": token}

    with pytest.raises(ReportError) as caught:
        async for _ in token_pages(fetch, invalid=INVALID):
            pass
    assert caught.value.message == INVALID.message
    assert len(asked) <= 2


async def test_token_pages_stop_after_max_pages() -> None:
    count = 0

    async def fetch(token: str | None) -> dict[str, Any]:
        nonlocal count
        count += 1
        return {"nextPageToken": f"t{count}"}

    with pytest.raises(ReportError):
        async for _ in token_pages(fetch, invalid=INVALID, max_pages=3):
            pass
    assert count <= 4


async def test_link_pages_follow_and_stop() -> None:
    pages: dict[str | None, dict[str, Any]] = {
        None: {"data": [1], "paging": {"next": "https://x/2"}},
        "https://x/2": {"data": [2], "paging": {"next": "https://x/3"}},
        "https://x/3": {"data": [3], "paging": {}},
    }
    asked: list[str | None] = []

    async def fetch(link: str | None) -> dict[str, Any]:
        asked.append(link)
        return pages[link]

    def next_link(body: dict[str, Any]) -> str | None:
        link = body.get("paging", {}).get("next")
        return link if isinstance(link, str) else None

    bodies = [b async for b in link_pages(fetch, next_link, invalid=INVALID)]
    assert [b["data"] for b in bodies] == [[1], [2], [3]]
    assert asked == [None, "https://x/2", "https://x/3"]


async def test_link_pages_refuse_a_repeated_link() -> None:
    asked: list[str | None] = []

    async def fetch(link: str | None) -> dict[str, Any]:
        asked.append(link)
        return {}

    with pytest.raises(ReportError):
        async for _ in link_pages(fetch, lambda body: "https://x/same", invalid=INVALID):
            pass
    assert asked == [None, "https://x/same"]


async def test_link_pages_stop_after_max_pages() -> None:
    count = 0

    async def fetch(link: str | None) -> dict[str, Any]:
        nonlocal count
        count += 1
        return {"next": f"https://x/{count}"}

    with pytest.raises(ReportError):
        async for _ in link_pages(fetch, lambda body: body["next"], invalid=INVALID, max_pages=2):
            pass
    assert count <= 3


# --- windows and days -----------------------------------------------------------------------


def test_windows_cover_the_range_without_gaps() -> None:
    assert list(windows(date(2026, 8, 1), date(2026, 10, 1), 30)) == [
        (date(2026, 8, 1), date(2026, 8, 30)),
        (date(2026, 8, 31), date(2026, 9, 29)),
        (date(2026, 9, 30), date(2026, 10, 1)),
    ]
    assert list(windows(date(2026, 1, 1), date(2026, 1, 1), 30)) == [
        (date(2026, 1, 1), date(2026, 1, 1))
    ]
    assert list(windows(date(2026, 1, 2), date(2026, 1, 1), 7)) == []
    assert list(windows(date(2026, 1, 1), date(2026, 1, 14), 7)) == [
        (date(2026, 1, 1), date(2026, 1, 7)),
        (date(2026, 1, 8), date(2026, 1, 14)),
    ]


@pytest.mark.parametrize("days", [0, -1])
def test_windows_refuse_less_than_a_day(days: int) -> None:
    with pytest.raises(ValueError):
        list(windows(date(2026, 1, 1), date(2026, 1, 2), days))


def test_each_day() -> None:
    assert list(each_day(date(2026, 2, 27), date(2026, 3, 1))) == [
        date(2026, 2, 27),
        date(2026, 2, 28),
        date(2026, 3, 1),
    ]
    assert list(each_day(date(2026, 3, 2), date(2026, 3, 1))) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("20261022", date(2026, 10, 22)),
        ("2026-10-22", date(2026, 10, 22)),
        ("20261322", None),
        ("2026-02-30", None),
        ("22/10/2026", None),
        ("", None),
        ("(other)", None),
    ],
)
def test_parse_day(value: str, expected: date | None) -> None:
    assert parse_day(value) == expected


# --- OAuth ----------------------------------------------------------------------------------


def id_token(claims: object) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def test_consent_url_encodes_params() -> None:
    url = consent_url("https://example.com/auth", scope="a b", state="x&y")
    assert url == "https://example.com/auth?scope=a+b&state=x%26y"


def test_email_from_id_token() -> None:
    assert email_from_id_token(id_token({"email": "reader@example.com"})) == "reader@example.com"
    assert email_from_id_token(id_token({"sub": "1"})) is None
    assert email_from_id_token(id_token({"email": 5})) is None
    assert email_from_id_token(id_token(["email"])) is None
    for bad in (None, "", 5, "no-dots", "a.!!!.c", "a.bm90IGpzb24.c"):
        assert email_from_id_token(bad) is None


def oauth(secret: str = "private-client-secret") -> GoogleOAuth:
    return GoogleOAuth("client-1", secret, "https://app.example.com/oauth/google/callback")


def test_secrets_stay_out_of_reprs() -> None:
    assert "private-client-secret" not in repr(oauth())
    assert "private-refresh" not in repr(GoogleGrant("private-refresh", "a@b.c"))


def test_configured_needs_id_and_secret() -> None:
    assert oauth().configured
    assert not oauth("").configured
    assert not GoogleOAuth("", "secret", "https://cb").configured


def test_authorize_url_asks_for_offline_consent_with_openid_email() -> None:
    url = oauth().authorize_url(SCOPE, "signed-state")
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == GOOGLE_AUTH_URL
    params = parse_qs(parsed.query)
    assert params == {
        "client_id": ["client-1"],
        "redirect_uri": ["https://app.example.com/oauth/google/callback"],
        "response_type": ["code"],
        "scope": [f"{SCOPE} openid email"],
        "access_type": ["offline"],
        "prompt": ["consent"],
        "state": ["signed-state"],
    }
    assert "private-client-secret" not in url


def token_endpoint(response: httpx.Response, seen: list[dict[str, list[str]]]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and str(request.url) == GOOGLE_TOKEN_URL
        seen.append(parse_qs(request.content.decode()))
        return response

    return handler


async def test_exchange_code_returns_the_refresh_token_and_email() -> None:
    seen: list[dict[str, list[str]]] = []
    body = {
        "access_token": "a",
        "refresh_token": "new-refresh",
        "scope": f"openid {SCOPE} https://www.googleapis.com/auth/userinfo.email",
        "id_token": id_token({"email": "reader@example.com"}),
    }
    handler = token_endpoint(httpx.Response(200, json=body), seen)
    grant = await oauth().exchange_code("the-code", SCOPE, transport=httpx.MockTransport(handler))
    assert grant == GoogleGrant("new-refresh", "reader@example.com")
    assert seen == [
        {
            "grant_type": ["authorization_code"],
            "code": ["the-code"],
            "client_id": ["client-1"],
            "client_secret": ["private-client-secret"],
            "redirect_uri": ["https://app.example.com/oauth/google/callback"],
        }
    ]


async def test_exchange_code_without_scope_or_id_token_is_accepted() -> None:
    handler = token_endpoint(httpx.Response(200, json={"refresh_token": "r"}), [])
    grant = await oauth().exchange_code("c", SCOPE, transport=httpx.MockTransport(handler))
    assert grant.refresh_token == "r" and grant.email is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad code"}),
        httpx.Response(401, json={"error": "invalid_client"}),
        httpx.Response(400),
        httpx.Response(400, text="<html>Bad request</html>"),
    ],
)
async def test_exchange_code_refused_is_an_auth_error(response: httpx.Response) -> None:
    refused: list[httpx.Response] = []
    handler = token_endpoint(response, [])
    with pytest.raises(ReportAuthError) as caught:
        await oauth().exchange_code(
            "c", SCOPE, transport=httpx.MockTransport(handler), on_error=refused.append
        )
    assert caught.value.reason == "auth_failed" and caught.value.auth
    assert "invalid_grant" not in str(caught.value) and "Bad code" not in str(caught.value)
    assert [r.status_code for r in refused] == [response.status_code]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"error": "internal"}),
        httpx.Response(503),
        httpx.Response(429, json={"error": "rate"}),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json=["refresh_token"]),
    ],
)
async def test_exchange_code_unavailable(response: httpx.Response) -> None:
    refused: list[httpx.Response] = []
    handler = token_endpoint(response, [])
    with pytest.raises(ReportError) as caught:
        await oauth().exchange_code(
            "c", SCOPE, transport=httpx.MockTransport(handler), on_error=refused.append
        )
    assert caught.value.reason == "unavailable" and not caught.value.auth
    assert len(refused) == (1 if response.is_error else 0)


async def test_exchange_code_network_error_is_unavailable_and_hides_the_cause() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom at the-code", request=request)

    with pytest.raises(ReportError) as caught:
        await oauth().exchange_code("the-code", SCOPE, transport=httpx.MockTransport(handler))
    assert caught.value.reason == "unavailable"
    assert caught.value.__cause__ is None and "the-code" not in str(caught.value)


@pytest.mark.parametrize("body", [{"access_token": "a"}, {"refresh_token": ""}])
async def test_exchange_code_without_a_refresh_token(body: dict[str, Any]) -> None:
    handler = token_endpoint(httpx.Response(200, json=body), [])
    with pytest.raises(ReportError) as caught:
        await oauth().exchange_code("c", SCOPE, transport=httpx.MockTransport(handler))
    assert caught.value.reason == "no_refresh_token" and not caught.value.auth


@pytest.mark.parametrize("granted", ["openid email", f"{SCOPE}.extra openid", 5])
async def test_exchange_code_without_the_scope_asked_for(granted: object) -> None:
    body = {"refresh_token": "r", "scope": granted}
    handler = token_endpoint(httpx.Response(200, json=body), [])
    with pytest.raises(ReportError) as caught:
        await oauth().exchange_code("c", SCOPE, transport=httpx.MockTransport(handler))
    assert caught.value.reason == "access_denied" and not caught.value.auth


def token_api(handler: Handler, sleep: Sleeper | None = None) -> ApiClient:
    def classify(response: httpx.Response) -> Verdict:
        if response.status_code >= 500:
            return Retry(UNAVAILABLE)
        return AUTH_FAILED if response.is_error else None

    return api(handler, sleep or Sleeper(), classify)


async def test_access_token_posts_a_refresh_grant() -> None:
    seen: list[dict[str, list[str]]] = []
    handler = token_endpoint(httpx.Response(200, json={"access_token": "fresh"}), seen)
    async with token_api(handler) as client:
        assert await oauth().access_token(client, "the-refresh", AUTH_FAILED) == "fresh"
    assert seen == [
        {
            "grant_type": ["refresh_token"],
            "refresh_token": ["the-refresh"],
            "client_id": ["client-1"],
            "client_secret": ["private-client-secret"],
        }
    ]


async def test_access_token_request_is_retried() -> None:
    handler, seen = replay(httpx.Response(503), httpx.Response(200, json={"access_token": "t"}))
    sleep = Sleeper()
    async with token_api(handler, sleep) as client:
        assert await oauth().access_token(client, "r", AUTH_FAILED) == "t"
    assert len(seen) == 2 and sleep.waits == [0.5]


@pytest.mark.parametrize("refresh_token", [None, "", 5, {"refresh_token": "x"}])
async def test_access_token_without_a_refresh_token_needs_authorizing_again(
    refresh_token: object,
) -> None:
    handler, seen = replay(httpx.Response(200, json={"access_token": "t"}))
    async with token_api(handler) as client:
        with pytest.raises(ReportAuthError) as caught:
            await oauth().access_token(client, refresh_token, AUTH_FAILED)
    assert caught.value.reason == "auth_failed"
    assert seen == []


async def test_access_token_when_the_client_is_not_configured() -> None:
    handler, seen = replay(httpx.Response(200, json={"access_token": "t"}))
    async with token_api(handler) as client:
        with pytest.raises(ReportError) as caught:
            await oauth("").access_token(client, "r", AUTH_FAILED)
    assert caught.value.reason == "oauth_not_configured" and not caught.value.auth
    assert seen == []


@pytest.mark.parametrize("body", [{}, {"access_token": ""}, {"access_token": 5}])
async def test_access_token_missing_from_the_answer_is_the_rejected_problem(
    body: dict[str, Any],
) -> None:
    rejected = Problem("auth_failed", "Google did not return an access token.")
    handler, _ = replay(httpx.Response(200, json=body))
    async with token_api(handler) as client:
        with pytest.raises(ReportAuthError) as caught:
            await oauth().access_token(client, "r", rejected)
    assert caught.value.message == rejected.message


async def test_access_token_refused_follows_the_clients_rules() -> None:
    handler, _ = replay(httpx.Response(400, json={"error": "invalid_grant"}))
    async with token_api(handler) as client:
        with pytest.raises(ReportAuthError):
            await oauth().access_token(client, "r", AUTH_FAILED)
