"""Facebook Pages and Instagram (organic Meta) against `httpx.MockTransport`."""

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
from data_sources.reports.sources import meta_organic as organic
from data_sources.reports.sources.meta_ads import appsecret_proof
from data_sources.reports.sources.meta_organic import (
    FACEBOOK_SCOPES,
    INSTAGRAM_SCOPES,
    FacebookPagesReports,
    InstagramReports,
)

APP_ID, APP_SECRET = "app-id", "app-secret"
REDIRECT = "https://app.example/api/v1/oauth/meta/callback"
USER_TOKEN = "user-token"
PAGE_TOKEN = "page-token"
PAGE, IG = "1111", "2222"
TODAY = date(2026, 10, 27)
CREDENTIALS = {"access_token": USER_TOKEN}

Handler = Callable[[httpx.Request], httpx.Response]


async def no_wait(_: float) -> None:
    pass


def make(provider: str, handler: Handler) -> Any:
    config = ConnectorConfig(
        provider=provider,
        auth=AuthConfig(
            type="oauth2",
            credentials={"app_id": APP_ID, "app_secret": APP_SECRET, "redirect_uri": REDIRECT},
        ),
    )
    return create_report_source(config, transport=httpx.MockTransport(handler), sleep=no_wait)


def facebook(handler: Handler) -> FacebookPagesReports:
    source = make("facebook_pages", handler)
    assert isinstance(source, FacebookPagesReports)
    return source


def instagram(handler: Handler) -> InstagramReports:
    source = make("instagram", handler)
    assert isinstance(source, InstagramReports)
    return source


def graph_error(
    code: int, message: str = "bad", status: int = 400, sub: int | None = None
) -> httpx.Response:
    error: dict[str, Any] = {"code": code, "message": message, "fbtrace_id": "trace-1"}
    if sub:
        error["error_subcode"] = sub
    return httpx.Response(status, json={"error": error})


def entry(
    name: str, values: list[tuple[str, Any]] | None = None, total: Any = None
) -> httpx.Response:
    item: dict[str, Any] = {"name": name, "period": "day"}
    if values is not None:
        item["values"] = [{"value": v, "end_time": t} for t, v in values]
    if total is not None:
        item["total_value"] = {"value": total}
    return httpx.Response(200, json={"data": [item]})


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(organic, "_today", lambda: TODAY)


# --- sign-in ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "scopes"), [("facebook_pages", FACEBOOK_SCOPES), ("instagram", INSTAGRAM_SCOPES)]
)
def test_each_source_asks_for_its_own_permissions_on_the_same_app(
    provider: str, scopes: tuple[str, ...]
) -> None:
    url = urlsplit(make(provider, lambda _: pytest.fail("no request")).authorize_url("state-1"))
    query = dict(parse_qsl(url.query))
    assert url.netloc == "www.facebook.com" and url.path.endswith("/dialog/oauth")
    assert query["client_id"] == APP_ID and query["redirect_uri"] == REDIRECT
    assert query["scope"] == ",".join(scopes) and query["state"] == "state-1"
    assert APP_SECRET not in url.query


async def test_the_code_is_exchanged_like_the_ads_one_for_a_long_lived_token() -> None:
    posts: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            form = dict(parse_qsl(request.content.decode()))
            posts.append(form)
            token = "long" if form.get("grant_type") == "fb_exchange_token" else "short"
            return httpx.Response(200, json={"access_token": token, "expires_in": 5184000})
        return httpx.Response(200, json={"name": "Ionut"})

    grant = await facebook(handler).exchange_code("the-code")
    assert [p.get("grant_type", "code") for p in posts] == ["code", "fb_exchange_token"]
    assert grant.credentials == {"access_token": "long"}
    assert grant.identity == "Ionut" and grant.expires_in == 5184000


async def test_facebook_lists_the_pages_the_user_manages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/me/accounts")
        assert request.headers["Authorization"] == f"Bearer {USER_TOKEN}"
        assert USER_TOKEN not in str(request.url)
        assert request.url.params["appsecret_proof"] == appsecret_proof(USER_TOKEN, APP_SECRET)
        return httpx.Response(
            200, json={"data": [{"id": PAGE, "name": "Shop", "category": "Retail"}]}
        )

    (account,) = await facebook(handler).list_accounts(CREDENTIALS)
    assert (account.id, account.name, account.details["category"]) == (PAGE, "Shop", "Retail")


async def test_instagram_lists_only_pages_that_have_a_linked_account() -> None:
    pages = [
        {"id": PAGE, "name": "Shop", "instagram_business_account": {"id": IG, "username": "shop"}},
        {"id": "3333", "name": "No Instagram"},
    ]
    (account,) = await instagram(lambda _: httpx.Response(200, json={"data": pages})).list_accounts(
        CREDENTIALS
    )
    assert (account.id, account.name, account.group) == (IG, "@shop", "Shop")
    assert account.details["page_id"] == PAGE


# --- Facebook Pages ---------------------------------------------------------------------------


class PageGraph:
    """The Graph API as a Page sees it: the page token, insights by metric, posts."""

    def __init__(
        self, invalid: set[str] | None = None, insights: dict[str, Any] | None = None
    ) -> None:
        self.invalid = invalid or set()
        self.requests: list[httpx.Request] = []
        self.insights = insights or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, params = request.url.path.removeprefix("/v26.0"), dict(request.url.params)
        if path == f"/{PAGE}" and params.get("fields") == "access_token":
            assert request.headers["Authorization"] == f"Bearer {USER_TOKEN}"
            return httpx.Response(200, json={"access_token": PAGE_TOKEN, "id": PAGE})
        # Everything else is read with the Page's own token.
        assert request.headers["Authorization"] == f"Bearer {PAGE_TOKEN}"
        assert params["appsecret_proof"] == appsecret_proof(PAGE_TOKEN, APP_SECRET)
        if path == f"/{PAGE}":
            return httpx.Response(200, json={"id": PAGE, "name": "Shop", "followers_count": 12})
        if path == f"/{PAGE}/insights":
            metric = params["metric"]
            if metric in self.invalid:
                return graph_error(
                    100, f"(#100) The value must be a valid insights metric: {metric}"
                )
            return self.insights.get(metric) or entry(
                metric, [("2026-10-21T07:00:00+0000", 5), ("2026-10-22T07:00:00+0000", 6)]
            )
        if path == f"/{PAGE}/posts":
            return httpx.Response(200, json={"data": self.posts(), "paging": {}})
        if path.endswith("/insights"):  # a post's
            metric = params["metric"]
            if metric in self.invalid:
                return graph_error(100, "invalid")
            return httpx.Response(200, json={"data": [{"name": metric, "values": [{"value": 7}]}]})
        pytest.fail(f"unexpected request {request.url}")

    @staticmethod
    def posts() -> list[dict[str, Any]]:
        return [
            {"id": f"{PAGE}_1", "created_time": "2026-10-20T10:00:00+0000", "message": "new"},
            {"id": f"{PAGE}_2", "created_time": "2026-06-01T10:00:00+0000", "message": "too old"},
        ]


async def test_page_insights_are_read_one_metric_at_a_time_with_the_pages_own_token() -> None:
    graph = PageGraph()
    report = await facebook(graph).read(
        CREDENTIALS, PAGE, {"report": "daily"}, date(2026, 10, 21), date(2026, 10, 22)
    )
    asked = [r.url.params for r in graph.requests if r.url.path.endswith("/insights")]
    assert [p["metric"] for p in asked] == list(organic.PAGE_METRICS)
    # Asking up to the day after `until` is what makes the last day come back.
    assert all(
        p["period"] == "day" and p["since"] == "2026-10-21" and p["until"] == "2026-10-23"
        for p in asked
    )
    assert [r["day"] for r in report.records] == [
        "2026-10-21"
    ]  # the 22nd's value ends on the 22nd: the 21st


async def test_daily_values_are_labelled_with_the_day_they_are_for() -> None:
    values = [
        ("2026-10-20T07:00:00+0000", 1),  # ends on the 20th: the 19th, before the window
        ("2026-10-21T07:00:00+0000", 2),  # the 20th, before the window
        ("2026-10-22T07:00:00+0000", 3),  # the 21st
        ("2026-10-23T07:00:00+0000", 4),  # the 22nd
    ]
    graph = PageGraph(insights={"page_media_view": entry("page_media_view", values)})
    report = await facebook(graph).read(
        CREDENTIALS, PAGE, {"report": "daily"}, date(2026, 10, 21), date(2026, 10, 22)
    )
    by_day = {r["day"]: r["metrics"]["page_media_view"] for r in report.records}
    assert by_day == {"2026-10-21": 3, "2026-10-22": 4}
    assert report.days == [date(2026, 10, 21), date(2026, 10, 22)]
    assert report.envelope["account"] == PAGE and report.envelope["report"] == "daily"


async def test_a_metric_meta_no_longer_has_is_skipped_and_named_not_fatal() -> None:
    graph = PageGraph(invalid={"page_video_views", "page_views_total"})
    report = await facebook(graph).read(
        CREDENTIALS, PAGE, {"report": "daily"}, date(2026, 10, 21), date(2026, 10, 22)
    )
    assert set(report.envelope["skipped"]) == {"page_video_views", "page_views_total"}
    assert "valid insights metric" in report.envelope["skipped"]["page_video_views"]
    assert all("page_video_views" not in r["metrics"] for r in report.records)
    assert report.records and "page_media_view" in report.records[0]["metrics"]


async def test_a_report_in_which_every_metric_was_refused_fails_with_metas_reason() -> None:
    graph = PageGraph(invalid=set(organic.PAGE_METRICS))
    with pytest.raises(ReportError) as caught:
        await facebook(graph).read(CREDENTIALS, PAGE, {"report": "daily"}, TODAY, TODAY)
    assert caught.value.reason == "request_failed"
    assert "100: (#100)" in caught.value.diagnostics and "trace-1" in caught.value.diagnostics
    assert "valid insights metric" not in caught.value.message


async def test_a_long_window_is_read_in_ninety_day_pieces() -> None:
    graph = PageGraph()
    await facebook(graph).read(
        CREDENTIALS, PAGE, {"report": "daily"}, date(2026, 5, 1), date(2026, 10, 26)
    )
    windows = {
        (r.url.params["since"], r.url.params["until"])
        for r in graph.requests
        if r.url.path.endswith("/insights")
    }
    assert windows == {("2026-05-01", "2026-07-30"), ("2026-07-30", "2026-10-27")}


async def test_the_page_snapshot_and_recent_posts_with_their_insights() -> None:
    graph = PageGraph()
    page = await facebook(graph).read(CREDENTIALS, PAGE, {"report": "page"}, TODAY, TODAY)
    assert page.records == [{"id": PAGE, "name": "Shop", "followers_count": 12}] and page.days == [
        TODAY
    ]
    posts = await facebook(graph).read(CREDENTIALS, PAGE, {"report": "posts"}, TODAY, TODAY)
    assert [p["id"] for p in posts.records] == [
        f"{PAGE}_1"
    ]  # the post of June is older than 90 days
    assert posts.records[0]["insights"] == {m: 7 for m in organic.POST_METRICS}
    assert posts.days == [TODAY]


async def test_a_page_the_user_cannot_manage_is_auth_failed() -> None:
    handler = lambda _: httpx.Response(200, json={"id": PAGE})  # noqa: E731
    with pytest.raises(ReportAuthError):
        await facebook(handler).read(CREDENTIALS, PAGE, {"report": "page"}, TODAY, TODAY)


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (graph_error(190, "token expired", 401), "auth_failed"),
        (graph_error(200, "permission", 403), "auth_failed"),
        (graph_error(4, "slow down"), "unavailable"),
        (graph_error(1, "unknown", 500), "unavailable"),
        (graph_error(2, "service", 400), "unavailable"),
    ],
)
async def test_failures_are_classified(response: httpx.Response, reason: str) -> None:
    with pytest.raises(ReportError) as caught:
        await facebook(lambda _: response).list_accounts(CREDENTIALS)
    assert caught.value.reason == reason


async def test_unsupported_reports_and_unbound_accounts_never_reach_meta() -> None:
    source = facebook(lambda _: pytest.fail("no request expected"))
    with pytest.raises(ReportError) as caught:
        await source.read(CREDENTIALS, PAGE, {"report": "messages"}, TODAY, TODAY)
    assert caught.value.reason == "request_failed"
    with pytest.raises(ReportError):
        await source.read(CREDENTIALS, "act_1", {"report": "page"}, TODAY, TODAY)


# --- Instagram --------------------------------------------------------------------------------


class IgGraph:
    def __init__(self, invalid: set[str] | None = None, followers: int = 500) -> None:
        self.invalid = invalid or set()
        self.requests: list[httpx.Request] = []
        self.followers = followers

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, params = request.url.path.removeprefix("/v26.0"), dict(request.url.params)
        assert request.headers["Authorization"] == f"Bearer {USER_TOKEN}"
        if path == f"/{IG}":
            return httpx.Response(200, json={"id": IG, "username": "shop", "followers_count": 500})
        if path == f"/{IG}/media":
            return httpx.Response(200, json={"data": self.media(), "paging": {}})
        if path.endswith("/insights") and path != f"/{IG}/insights":  # a media's
            metric = params["metric"]
            if metric in self.invalid:
                return graph_error(100, f"metric {metric} not supported for this media")
            return httpx.Response(200, json={"data": [{"name": metric, "values": [{"value": 9}]}]})
        assert path == f"/{IG}/insights"
        metric = params["metric"]
        if metric in self.invalid:
            return graph_error(100, f"invalid metric {metric}")
        if metric == "follower_demographics":
            if self.followers < 100:
                return graph_error(100, "not enough followers", sub=2108006)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "name": metric,
                            "total_value": {
                                "breakdowns": [
                                    {
                                        "dimension_keys": [params["breakdown"]],
                                        "results": [
                                            {"dimension_values": ["F", "25-34"], "value": 11},
                                            {"dimension_values": ["M"], "value": 4},
                                        ],
                                    }
                                ]
                            },
                        }
                    ]
                },
            )
        if params["metric_type"] == "time_series":
            return entry(
                metric, [("2026-10-22T07:00:00+0000", 40), ("2026-10-23T07:00:00+0000", 41)]
            )
        assert (
            date.fromisoformat(params["until"]).toordinal()
            == date.fromisoformat(params["since"]).toordinal() + 1
        )
        return entry(metric, total=3)

    @staticmethod
    def media() -> list[dict[str, Any]]:
        return [
            {"id": "m1", "media_product_type": "FEED", "timestamp": "2026-10-20T10:00:00+0000"},
            {"id": "m2", "media_product_type": "REELS", "timestamp": "2026-10-10T10:00:00+0000"},
            {"id": "m3", "media_product_type": "FEED", "timestamp": "2026-03-01T10:00:00+0000"},
        ]


async def test_account_insights_are_a_series_where_meta_has_one_and_a_day_at_a_time_where_not() -> (
    None
):
    graph = IgGraph()
    report = await instagram(graph).read(
        CREDENTIALS, IG, {"report": "daily"}, date(2026, 10, 21), date(2026, 10, 22)
    )
    asked = [(r.url.params["metric"], r.url.params["metric_type"]) for r in graph.requests]
    assert asked[:2] == [("reach", "time_series"), ("follower_count", "time_series")]
    # Totals: every metric for each of the two days.
    assert len([a for a in asked if a[1] == "total_value"]) == 2 * len(organic.IG_TOTAL_METRICS)
    by_day = {r["day"]: r["metrics"] for r in report.records}
    assert by_day["2026-10-21"]["views"] == 3 and by_day["2026-10-22"]["reach"] == 41
    assert by_day["2026-10-21"]["follower_count"] == 40 and report.days == [
        date(2026, 10, 21),
        date(2026, 10, 22),
    ]


async def test_instagram_series_requests_are_cut_to_thirty_days() -> None:
    graph = IgGraph(
        invalid=set(organic.IG_TOTAL_METRICS)
    )  # the totals are one request a day: not what is tested
    with pytest.raises(ReportError):
        await instagram(graph).read(
            CREDENTIALS, IG, {"report": "daily"}, date(2026, 8, 1), date(2026, 10, 26)
        )
    series = {
        (r.url.params["since"], r.url.params["until"])
        for r in graph.requests
        if r.url.params["metric_type"] == "time_series"
    }
    assert series == {
        ("2026-08-01", "2026-08-31"),
        ("2026-08-31", "2026-09-30"),
        ("2026-09-30", "2026-10-27"),
    }


async def test_an_account_without_a_metric_still_reports_the_rest() -> None:
    graph = IgGraph(invalid={"follower_count", "replies"})
    report = await instagram(graph).read(CREDENTIALS, IG, {"report": "daily"}, TODAY, TODAY)
    assert set(report.envelope["skipped"]) == {"follower_count", "replies"}
    assert all("replies" not in r["metrics"] for r in report.records)


async def test_the_account_snapshot() -> None:
    report = await instagram(IgGraph()).read(CREDENTIALS, IG, {"report": "account"}, TODAY, TODAY)
    assert report.records[0]["followers_count"] == 500 and report.days == [TODAY]


async def test_recent_media_get_their_types_metrics_and_older_media_end_the_paging() -> None:
    graph = IgGraph(invalid={"ig_reels_avg_watch_time"})
    report = await instagram(graph).read(CREDENTIALS, IG, {"report": "media"}, TODAY, TODAY)
    assert [m["id"] for m in report.records] == ["m1", "m2"]
    feed, reel = report.records
    assert set(feed["insights"]) == set(organic.IG_MEDIA_METRICS["FEED"])
    assert (
        "ig_reels_video_view_total_time" in reel["insights"]
        and "ig_reels_avg_watch_time" not in reel["insights"]
    )
    assert report.envelope["skipped"].keys() == {"ig_reels_avg_watch_time"}
    assert report.days == [TODAY, TODAY]


async def test_a_media_that_has_no_insights_is_kept_without_them() -> None:
    graph = IgGraph(
        invalid=set(organic.IG_MEDIA_METRICS["FEED"]) | set(organic.IG_MEDIA_METRICS["REELS"])
    )
    report = await instagram(graph).read(CREDENTIALS, IG, {"report": "media"}, TODAY, TODAY)
    assert [m["insights"] for m in report.records] == [{}, {}]
    assert "*" in report.envelope["skipped"]


async def test_audience_has_a_record_per_breakdown_value() -> None:
    report = await instagram(IgGraph()).read(CREDENTIALS, IG, {"report": "audience"}, TODAY, TODAY)
    assert len(report.records) == 2 * len(organic.IG_AUDIENCE_BREAKDOWNS)
    assert {"breakdown": "age", "dimension": "F/25-34", "value": 11} in report.records
    assert report.days == [TODAY] * len(report.records) and report.envelope["skipped"] == {}


async def test_a_small_account_has_no_audience_data_and_that_is_not_an_error() -> None:
    report = await instagram(IgGraph(followers=40)).read(
        CREDENTIALS, IG, {"report": "audience"}, TODAY, TODAY
    )
    assert report.records == [] and len(report.envelope["skipped"]) == len(
        organic.IG_AUDIENCE_BREAKDOWNS
    )


async def test_an_expired_token_fails_the_audience_report_instead_of_skipping() -> None:
    handler = lambda _: graph_error(190, "expired", 401)  # noqa: E731
    with pytest.raises(ReportAuthError):
        await instagram(handler).read(CREDENTIALS, IG, {"report": "audience"}, TODAY, TODAY)


def test_the_organic_sources_are_registered_and_the_shared_base_is_not() -> None:
    from data_sources.reports import report_sources

    providers = report_sources.providers()
    assert {"facebook_pages", "instagram"} <= set(providers) and "meta_organic" not in providers
    assert json.dumps(sorted(providers))  # the registry still loads every module
