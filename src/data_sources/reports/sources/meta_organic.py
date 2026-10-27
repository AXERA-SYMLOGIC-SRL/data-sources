"""Facebook Pages and Instagram, the organic side of Meta (not the ads in `meta_ads`), through the
Graph API.

Both are signed into with Facebook Login on the same Meta app as `meta_ads`: the same app id,
secret and redirect URI, with more permissions asked for. The consent buys a long-lived user token
(about 60 days, then the user consents again); a Page's own access token, which Page insights need,
is asked for with it on every read.

An account is a Facebook Page (`facebook_pages`) or an Instagram professional account (`instagram`,
one that is linked to a Page the user manages). Their reports are of two kinds:

- *daily* reports, one record per day with `{"metrics": {name: value}}`, read for the window asked
  for;
- *snapshots* (the account's counters, its recent posts with theirs, an audience), which Meta only
  gives as they are now: read as of today, every record labelled with today, so an application that
  reads them every day builds history.

Meta changes which metrics exist often (impressions and fans were replaced in 2025, reach again in
2026), so a metric is asked for on its own and one Meta says is invalid is skipped and named in the
envelope's `skipped`, instead of failing the report. A report in which every metric was refused
fails with Meta's own reason.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any, ClassVar

import httpx

from data_sources.core.logging import logger
from data_sources.reports.base import Account, Grant, ReportSource
from data_sources.reports.errors import Problem, ReportAuthError, ReportError
from data_sources.reports.http import ApiClient, Retry, Verdict, exponential
from data_sources.reports.models import RawReport
from data_sources.reports.oauth import consent_url
from data_sources.reports.sources.meta_ads import (
    AUTH_CODES,
    THROTTLE_CODES,
    MetaAdsReports,
    appsecret_proof,
)
from data_sources.reports.windows import windows

FACEBOOK_SCOPES = ("pages_show_list", "pages_read_engagement", "read_insights")
INSTAGRAM_SCOPES = (
    "instagram_basic",
    "instagram_manage_insights",
    "pages_show_list",
    "pages_read_engagement",
)

#: Page insights read per day. `page_media_view` replaced impressions,
#: `page_total_media_view_unique` replaced unique
#: impressions (reach) and `page_follows` replaced fans.
PAGE_METRICS = (
    "page_media_view",
    "page_total_media_view_unique",
    "page_post_engagements",
    "page_follows",
    "page_daily_follows_unique",
    "page_daily_unfollows_unique",
    "page_views_total",
    "page_video_views",
)
POST_METRICS = ("post_media_view", "post_total_media_view_unique", "post_clicks")
PAGE_FIELDS = "id,name,username,link,category,about,website,followers_count,fan_count"
POST_FIELDS = (
    "id,message,created_time,permalink_url,status_type,shares,"
    "reactions.summary(total_count).limit(0),comments.summary(total_count).limit(0)"
)

#: Instagram account insights per day: those Meta can give as a daily series, and those it only
#: gives as a total over
#: a range (asked for a day at a time).
IG_SERIES_METRICS = ("reach", "follower_count")
IG_TOTAL_METRICS = (
    "views",
    "profile_views",
    "accounts_engaged",
    "total_interactions",
    "likes",
    "comments",
    "shares",
    "saves",
    "replies",
    "profile_links_taps",
)
IG_ACCOUNT_FIELDS = "id,username,name,biography,website,followers_count,follows_count,media_count"
IG_MEDIA_FIELDS = (
    "id,caption,media_type,media_product_type,permalink,timestamp,"
    "like_count,comments_count,thumbnail_url"
)
IG_MEDIA_METRICS = {
    "FEED": ("reach", "views", "saved", "shares", "likes", "comments", "total_interactions"),
    "REELS": (
        "reach",
        "views",
        "saved",
        "shares",
        "likes",
        "comments",
        "total_interactions",
        "ig_reels_avg_watch_time",
        "ig_reels_video_view_total_time",
    ),
}
IG_AUDIENCE_BREAKDOWNS = ("age", "gender", "country", "city")
#: An account's follower demographics, `metric_type=total_value`, one breakdown at a time.
IG_SERIES_WINDOW_DAYS = 30
FB_SERIES_WINDOW_DAYS = 90

#: Posts and media whose counters are read: those published in the last days (older ones hardly
#: change, and reading
#: every post of a large account would take hours).
RECENT_DAYS = 90
PAGE_SIZE = 50
MAX_PAGES = 1000

UNAVAILABLE = Problem("unavailable", "Meta couldn't be reached or refused the request.")
BUSY = Problem("unavailable", "Meta kept throttling the request; try again later.")
INVALID = Problem("unavailable", "Meta returned an invalid response.")
AUTH_FAILED = Problem(
    "auth_failed", "Meta refused the token: it is invalid, expired or missing a permission."
)

_logger = logger.getChild("meta_organic.reports")


def _today() -> date:
    return datetime.now(UTC).date()


def _error_parts(response: httpx.Response) -> tuple[int | None, int | None, str, str]:
    """`(code, subcode, message, fbtrace_id)` of a Graph error body."""
    try:
        error = response.json()["error"]
    except (ValueError, KeyError, TypeError):
        return None, None, "", ""
    if not isinstance(error, dict):
        return None, None, "", ""
    code, sub = error.get("code"), error.get("error_subcode")
    return (
        code if isinstance(code, int) else None,
        sub if isinstance(sub, int) else None,
        " ".join(str(error.get("message") or "").split())[:300],
        str(error.get("fbtrace_id") or "")[:60],
    )


def classify(response: httpx.Response) -> Verdict:
    """Meta's rules (throttles retried, bad tokens and missing permissions are the grant's fault),
    except that a refused request (error 100: an invalid metric, a metric the account's type
    doesn't have) is `request_failed` with Meta's words in the diagnostics, which is what lets a
    report skip one metric."""
    if response.status_code < 400:
        return None
    code, subcode, message, trace = _error_parts(response)
    if code in THROTTLE_CODES or response.status_code >= 500:
        return Retry(BUSY)
    if code in AUTH_CODES or response.status_code in (401, 403):
        return Problem("auth_failed", AUTH_FAILED.message, message)
    detail = f"{code}/{subcode}: {message}" if subcode else f"{code}: {message}"
    diagnostics = f"{detail} (trace {trace})" if trace else detail
    if code == 100:
        return Problem("request_failed", "Meta refused the request.", diagnostics)
    return Problem("unavailable", UNAVAILABLE.message, diagnostics)


class MetaOrganicReports(ReportSource):
    """What Facebook Pages and Instagram share: sign-in, listing the Pages a user manages, one-
    metric-at-a-time insights and cursor paging. Abstract: `FacebookPagesReports` and
    `InstagramReports` are the sources."""

    app_credentials = ("app_id", "app_secret")
    scopes: ClassVar[tuple[str, ...]]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # The token exchange is the same as the ads one: short-lived, then long-lived.
        self._ads = MetaAdsReports(self.config, transport=self.transport, sleep=self.sleep)

    @property
    def graph_url(self) -> str:
        return self._ads.graph_url

    def _client(self, timeout: float = 60) -> ApiClient:
        return ApiClient(
            classify,
            unreachable=UNAVAILABLE,
            invalid=INVALID,
            timeout=timeout,
            attempts=5,
            backoff=exponential(2.0),
            transport=self.transport,
            sleep=self.sleep,
        )

    @staticmethod
    def _token(credentials: Mapping[str, Any]) -> str:
        token = credentials.get("access_token")
        if not isinstance(token, str) or not token:
            raise AUTH_FAILED.error()
        return token

    async def _get(
        self, api: ApiClient, token: str, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        query = dict(params or {})
        if app_secret := self.credential("app_secret"):
            query["appsecret_proof"] = appsecret_proof(token, app_secret)
        return await api.json(
            "GET",
            f"{self.graph_url}/{path}",
            params=query,
            headers={"Authorization": f"Bearer {token}"},
        )

    async def _pages(
        self, api: ApiClient, token: str, path: str, params: dict[str, Any]
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Each page of `data` from a cursor-paged edge."""
        query = dict(params)
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            page = await self._get(api, token, path, query)
            data = page.get("data", [])
            if not isinstance(data, list):
                raise INVALID.error()
            yield data
            paging = page.get("paging", {})
            cursors = paging.get("cursors", {}) if isinstance(paging, dict) else {}
            after = cursors.get("after") if isinstance(cursors, dict) else None
            if not data or not after or "next" not in paging:
                return
            if not isinstance(after, str) or after in seen:
                raise INVALID.error()  # a cursor that doesn't move would page forever
            seen.add(after)
            query["after"] = after
        raise INVALID.error()

    # Sign-in

    def authorize_url(self, state: str) -> str:
        return consent_url(
            self._ads.dialog_url,
            client_id=self.credential("app_id"),
            redirect_uri=self.credential("redirect_uri"),
            state=state,
            scope=",".join(self.scopes),
            response_type="code",
        )

    async def exchange_code(self, code: str) -> Grant:
        """The long-lived user token the consent's `code` buys (`{"access_token"}`), how long it
        lasts, and who gave it."""
        return await self._ads.exchange_code(code)

    # Metrics

    async def _insights(
        self,
        api: ApiClient,
        token: str,
        path: str,
        metrics: tuple[str, ...],
        params: dict[str, Any],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        """`({metric: its insights entry}, {metric: why it was skipped})`, asking for each metric
        alone. A metric
        Meta refuses is skipped; if none was accepted, the refusal is raised."""
        found: dict[str, dict[str, Any]] = {}
        skipped: dict[str, str] = {}
        first: ReportError | None = None
        for metric in metrics:
            try:
                body = await self._get(api, token, path, {**params, "metric": metric})
            except ReportError as exc:
                if exc.reason != "request_failed":
                    raise
                first = first or exc
                skipped[metric] = exc.diagnostics or exc.message
                continue
            entries = body.get("data")
            entry = entries[0] if isinstance(entries, list) and entries else None
            if isinstance(entry, dict):
                found[metric] = entry
        if not found and first is not None:
            raise first
        return found, skipped

    @staticmethod
    def _entry_day(end_time: Any) -> date | None:
        """The day a daily value is for: its `end_time` is the end of that day (midnight
        Pacific)."""
        try:
            return datetime.fromisoformat(
                str(end_time).replace("+0000", "+00:00")
            ).date() - timedelta(days=1)
        except ValueError:
            return None

    @classmethod
    def _series(cls, entry: dict[str, Any], since: date, until: date) -> dict[date, Any]:
        """The `{day: value}` of a daily series entry, inside `since..until`."""
        values: dict[date, Any] = {}
        for item in entry.get("values", []) if isinstance(entry.get("values"), list) else []:
            day = cls._entry_day(item.get("end_time")) if isinstance(item, dict) else None
            if day is not None and since <= day <= until and "value" in item:
                values[day] = item["value"]
        return values

    @staticmethod
    def _daily_records(
        by_metric: dict[str, dict[date, Any]],
    ) -> tuple[list[dict[str, Any]], list[date | None]]:
        days = sorted({day for values in by_metric.values() for day in values})
        records = [
            {
                "day": day.isoformat(),
                "metrics": {m: values[day] for m, values in by_metric.items() if day in values},
            }
            for day in days
        ]
        return records, list(days)

    @staticmethod
    def _created(item: Mapping[str, Any], key: str) -> date | None:
        try:
            return datetime.fromisoformat(str(item.get(key)).replace("+0000", "+00:00")).date()
        except ValueError:
            return None

    @staticmethod
    def _snapshot(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[date | None]]:
        return records, [_today()] * len(records)


class FacebookPagesReports(MetaOrganicReports):
    """Facebook Pages (`provider: facebook_pages`): `app_id`, `app_secret` and `redirect_uri` in
    `auth.credentials`. The grant's credentials are `{"access_token"}`; an account is a Page
    (its id); a report spec is `{"report": "daily" | "page" | "posts"}`."""

    provider = "facebook_pages"
    scopes = FACEBOOK_SCOPES
    REPORTS = ("daily", "page", "posts")

    async def list_pages(self, access_token: str) -> list[dict[str, Any]]:
        """The Pages the user manages, as Meta lists them."""
        token = self._token({"access_token": access_token})
        params = {
            "fields": "id,name,category,instagram_business_account{id,username}",
            "limit": 100,
        }
        async with self._client() as api:
            return [
                p async for page in self._pages(api, token, "me/accounts", params) for p in page
            ]

    async def list_accounts(self, credentials: Mapping[str, Any]) -> list[Account]:
        return [
            Account(
                id=str(p["id"]),
                name=str(p.get("name") or p["id"]),
                details={"category": p.get("category")},
            )
            for p in await self.list_pages(credentials.get("access_token"))  # type: ignore[arg-type]
        ]

    async def _page_token(self, api: ApiClient, user_token: str, page_id: str) -> str:
        body = await self._get(api, user_token, page_id, {"fields": "access_token"})
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise ReportAuthError(
                "auth_failed",
                "Meta gave no access to this Page: the user can't manage it (any more).",
            )
        return token

    async def read(
        self,
        credentials: Mapping[str, Any],
        account: str,
        spec: Mapping[str, Any],
        since: date,
        until: date,
    ) -> RawReport:
        report = spec.get("report")
        if report not in self.REPORTS:
            raise ReportError("request_failed", "Unsupported Facebook Pages report.")
        if not account.isdigit():
            raise ReportError("request_failed", "The source is not bound to a Facebook Page.")
        user_token = self._token(credentials)
        skipped: dict[str, str] = {}
        async with self._client() as api:
            token = await self._page_token(api, user_token, account)
            if report == "daily":
                records, days = await self._read_daily(api, token, account, since, until, skipped)
            elif report == "page":
                records, days = self._snapshot(
                    [await self._get(api, token, account, {"fields": PAGE_FIELDS})]
                )
            else:
                records, days = await self._read_posts(api, token, account, skipped)
        _logger.info(f"Read {len(records)} Facebook {report} records for {account}")
        return RawReport(records, days, {"account": account, "report": report, "skipped": skipped})

    async def _read_daily(
        self,
        api: ApiClient,
        token: str,
        page: str,
        since: date,
        until: date,
        skipped: dict[str, str],
    ) -> tuple[list[dict[str, Any]], list[date | None]]:
        by_metric: dict[str, dict[date, Any]] = {}
        for start, end in windows(since, until, FB_SERIES_WINDOW_DAYS):
            # The first value's end_time is `since` plus a day, so asking up to `until` plus a day
            # gives `until`.
            params = {
                "period": "day",
                "since": start.isoformat(),
                "until": (end + timedelta(days=1)).isoformat(),
            }
            found, refused = await self._insights(
                api, token, f"{page}/insights", PAGE_METRICS, params
            )
            skipped.update(refused)
            for metric, entry in found.items():
                by_metric.setdefault(metric, {}).update(self._series(entry, start, end))
        return self._daily_records(by_metric)

    async def _read_posts(
        self, api: ApiClient, token: str, page: str, skipped: dict[str, str]
    ) -> tuple[list[dict[str, Any]], list[date | None]]:
        cutoff = _today() - timedelta(days=RECENT_DAYS)
        posts: list[dict[str, Any]] = []
        params = {"fields": POST_FIELDS, "limit": PAGE_SIZE}
        async for batch in self._pages(api, token, f"{page}/posts", params):
            posts.extend(
                p for p in batch if (self._created(p, "created_time") or _today()) >= cutoff
            )
            if batch and (self._created(batch[-1], "created_time") or _today()) < cutoff:
                break  # newest first: everything after is older still
        for post in posts:
            if not post.get("id"):
                raise INVALID.error()
            found, refused = await self._insights(
                api, token, f"{post['id']}/insights", POST_METRICS, {"period": "lifetime"}
            )
            skipped.update(refused)
            post["insights"] = {m: self._lifetime(e) for m, e in found.items()}
        return self._snapshot(posts)

    @staticmethod
    def _lifetime(entry: dict[str, Any]) -> Any:
        values = entry.get("values")
        return (
            values[0].get("value")
            if isinstance(values, list) and values and isinstance(values[0], dict)
            else None
        )


class InstagramReports(MetaOrganicReports):
    """Instagram (`provider: instagram`), a professional (Business or Creator) account linked to a
    Facebook Page the user manages: `app_id`, `app_secret` and `redirect_uri` in
    `auth.credentials`. The grant's credentials are `{"access_token"}`; an account is the
    Instagram account's id, `group` the Page it is linked to; a report spec is `{"report":
    "daily" | "account" | "media" | "audience"}`."""

    provider = "instagram"
    scopes = INSTAGRAM_SCOPES
    REPORTS = ("daily", "account", "media", "audience")

    async def list_accounts(self, credentials: Mapping[str, Any]) -> list[Account]:
        """The Instagram accounts linked to the Pages the user manages."""
        token = self._token(credentials)
        params = {"fields": "id,name,instagram_business_account{id,username,name}", "limit": 100}
        accounts: list[Account] = []
        async with self._client() as api:
            async for batch in self._pages(api, token, "me/accounts", params):
                for page in batch:
                    ig = page.get("instagram_business_account")
                    if isinstance(ig, dict) and ig.get("id"):
                        handle = ig.get("username")
                        accounts.append(
                            Account(
                                id=str(ig["id"]),
                                name=f"@{handle}" if handle else str(ig.get("name") or ig["id"]),
                                group=str(page.get("name") or ""),
                                details={"page_id": page.get("id"), "username": handle},
                            )
                        )
        return accounts

    async def read(
        self,
        credentials: Mapping[str, Any],
        account: str,
        spec: Mapping[str, Any],
        since: date,
        until: date,
    ) -> RawReport:
        report = spec.get("report")
        if report not in self.REPORTS:
            raise ReportError("request_failed", "Unsupported Instagram report.")
        if not account.isdigit():
            raise ReportError("request_failed", "The source is not bound to an Instagram account.")
        token = self._token(credentials)
        skipped: dict[str, str] = {}
        async with self._client() as api:
            if report == "daily":
                records, days = await self._read_daily(api, token, account, since, until, skipped)
            elif report == "account":
                records, days = self._snapshot(
                    [await self._get(api, token, account, {"fields": IG_ACCOUNT_FIELDS})]
                )
            elif report == "media":
                records, days = await self._read_media(api, token, account, skipped)
            else:
                records, days = await self._read_audience(api, token, account, skipped)
        _logger.info(f"Read {len(records)} Instagram {report} records for {account}")
        return RawReport(records, days, {"account": account, "report": report, "skipped": skipped})

    async def _read_daily(
        self, api: ApiClient, token: str, ig: str, since: date, until: date, skipped: dict[str, str]
    ) -> tuple[list[dict[str, Any]], list[date | None]]:
        by_metric: dict[str, dict[date, Any]] = {}
        path = f"{ig}/insights"
        for start, end in windows(since, until, IG_SERIES_WINDOW_DAYS):
            params = {
                "period": "day",
                "metric_type": "time_series",
                "since": start.isoformat(),
                "until": (end + timedelta(days=1)).isoformat(),
            }
            found, refused = await self._insights(api, token, path, IG_SERIES_METRICS, params)
            skipped.update(refused)
            for metric, entry in found.items():
                by_metric.setdefault(metric, {}).update(self._series(entry, start, end))
        # The rest only come as a total over the range asked for, so a day at a time.
        day = since
        while day <= until:
            params = {
                "period": "day",
                "metric_type": "total_value",
                "since": day.isoformat(),
                "until": (day + timedelta(days=1)).isoformat(),
            }
            found, refused = await self._insights(api, token, path, IG_TOTAL_METRICS, params)
            skipped.update(refused)
            for metric, entry in found.items():
                total = entry.get("total_value")
                if isinstance(total, dict) and "value" in total:
                    by_metric.setdefault(metric, {})[day] = total["value"]
            day += timedelta(days=1)
        return self._daily_records(by_metric)

    async def _read_media(
        self, api: ApiClient, token: str, ig: str, skipped: dict[str, str]
    ) -> tuple[list[dict[str, Any]], list[date | None]]:
        cutoff = _today() - timedelta(days=RECENT_DAYS)
        media: list[dict[str, Any]] = []
        params = {"fields": IG_MEDIA_FIELDS, "limit": PAGE_SIZE}
        async for batch in self._pages(api, token, f"{ig}/media", params):
            media.extend(m for m in batch if (self._created(m, "timestamp") or _today()) >= cutoff)
            if batch and (self._created(batch[-1], "timestamp") or _today()) < cutoff:
                break  # newest first
        for item in media:
            if not item.get("id"):
                raise INVALID.error()
            metrics = IG_MEDIA_METRICS.get(
                str(item.get("media_product_type")), IG_MEDIA_METRICS["FEED"]
            )
            try:
                found, refused = await self._insights(
                    api, token, f"{item['id']}/insights", metrics, {}
                )
            except ReportError as exc:
                if exc.reason != "request_failed":
                    raise
                found, refused = (
                    {},
                    {"*": exc.diagnostics or exc.message},
                )  # e.g. an ad or an album: no insights
            skipped.update(refused)
            item["insights"] = {m: self._media_value(e) for m, e in found.items()}
        return self._snapshot(media)

    @staticmethod
    def _media_value(entry: dict[str, Any]) -> Any:
        values = entry.get("values")
        if isinstance(values, list) and values and isinstance(values[0], dict):
            return values[0].get("value")
        total = entry.get("total_value")
        return total.get("value") if isinstance(total, dict) else None

    async def _read_audience(
        self, api: ApiClient, token: str, ig: str, skipped: dict[str, str]
    ) -> tuple[list[dict[str, Any]], list[date | None]]:
        """Follower demographics: a record per breakdown value (`{"breakdown", "dimension",
        "value"}`). Meta only has
        them for accounts with at least 100 followers; for others the breakdowns are skipped, not
        failed."""
        records: list[dict[str, Any]] = []
        for breakdown in IG_AUDIENCE_BREAKDOWNS:
            params = {
                "metric": "follower_demographics",
                "period": "lifetime",
                "metric_type": "total_value",
                "breakdown": breakdown,
            }
            try:
                body = await self._get(api, token, f"{ig}/insights", params)
            except ReportError as exc:
                if exc.reason != "request_failed":
                    raise
                skipped[f"follower_demographics:{breakdown}"] = exc.diagnostics or exc.message
                continue
            entries = body.get("data")
            total = entries[0].get("total_value") if isinstance(entries, list) and entries else None
            for group in total.get("breakdowns", []) if isinstance(total, dict) else []:
                for result in group.get("results", []) if isinstance(group, dict) else []:
                    values = result.get("dimension_values") if isinstance(result, dict) else None
                    if isinstance(values, list) and values and "value" in result:
                        records.append(
                            {
                                "breakdown": breakdown,
                                "dimension": "/".join(map(str, values)),
                                "value": result["value"],
                            }
                        )
        return self._snapshot(records)
