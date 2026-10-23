from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from data_sources.config.schema import AuthConfig, ConnectorConfig
from data_sources.connectors.google_analytics import (
    DateRange,
    GoogleAnalyticsClient,
    GoogleAnalyticsConnector,
    ReportSpec,
)
from data_sources.connectors.google_analytics.connector import parse_property
from data_sources.core.exceptions import (
    AuthenticationError,
    ConfigurationError,
    ConnectionError,
    DataSourceError,
    NotFoundError,
    RateLimitError,
)
from data_sources.core.models import ItemType
from data_sources.core.registry import registry

PROPERTY = "properties/123456789"


class FakeCredential:
    async def get_token(self) -> str:
        return "fake-token"

    async def close(self) -> None:
        pass


class FakeAnalytics:
    """A GA4 property behind `httpx.MockTransport`: one table of `rows`, served in the
    pages (`limit`/`offset`) a `runReport` asks for."""

    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self.rows = rows
        self.requests: list[dict[str, Any]] = []

    def account_summaries(self, request: httpx.Request) -> httpx.Response:
        if request.url.params.get("pageToken") == "p2":
            return httpx.Response(
                200,
                json={
                    "accountSummaries": [
                        {
                            "account": "accounts/2",
                            "displayName": "Beta",
                            "propertySummaries": [
                                {"property": "properties/22", "displayName": "Beta site"}
                            ],
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "accountSummaries": [
                    {
                        "account": "accounts/1",
                        "displayName": "Alpha",
                        "propertySummaries": [
                            {"property": "properties/11", "displayName": "Alpha web"},
                            {"property": "properties/12", "displayName": "Alpha app"},
                        ],
                    },
                    {"account": "accounts/3", "displayName": "Empty"},
                ],
                "nextPageToken": "p2",
            },
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer fake-token"
        if request.url.path == "/v1beta/accountSummaries":
            return self.account_summaries(request)
        if request.url.path.endswith("/metadata"):
            return httpx.Response(200, json={"dimensions": [], "metrics": []})
        assert request.url.path == f"/v1beta/{PROPERTY}:runReport"
        body = json.loads(request.content)
        self.requests.append(body)
        offset, limit = body["offset"], body["limit"]
        page = self.rows[offset : offset + limit]
        return httpx.Response(
            200,
            json={
                "dimensionHeaders": [{"name": "country"}],
                "metricHeaders": [{"name": "sessions", "type": "TYPE_INTEGER"}],
                "rows": [
                    {"dimensionValues": [{"value": d}], "metricValues": [{"value": m}]}
                    for d, m in page
                ],
                "rowCount": len(self.rows),
            },
        )


ROWS = [("Romania", "10"), ("Germany", "7"), ("France, EU", "5")]
REPORTS = {
    "sessions-by-country": {"dimensions": ["country"], "metrics": ["sessions"]},
}


def make(
    fake: FakeAnalytics, *, page_size: int = 100, reports: Any = REPORTS
) -> GoogleAnalyticsConnector:
    connector = GoogleAnalyticsConnector(
        ConnectorConfig(
            provider="google_analytics",
            options={"property": "123456789", "reports": reports, "page_size": page_size},
        )
    )
    connector._client = GoogleAnalyticsClient(
        FakeCredential(), http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    )
    return connector


def test_registered() -> None:
    assert registry.get("google_analytics") is GoogleAnalyticsConnector


@pytest.mark.parametrize("value", ["123", "properties/123"])
def test_parse_property(value: str) -> None:
    assert parse_property(value) == "properties/123"


@pytest.mark.parametrize("value", ["G-ABC123", "properties/", "properties/abc", ""])
def test_parse_property_rejects_non_properties(value: str) -> None:
    with pytest.raises(ConfigurationError):
        parse_property(value)


@pytest.mark.parametrize(
    "options",
    [
        {"property": "bad"},
        {"property": "1", "reports": ["not", "a", "mapping"]},
        {"property": "1", "reports": {"r": {"metrics": []}}},
        {"property": "1", "reports": {"a/b": {"metrics": ["sessions"]}}},
        {"property": "1", "page_size": 0},
    ],
)
def test_bad_config(options: dict[str, Any]) -> None:
    with pytest.raises(ConfigurationError):
        GoogleAnalyticsConnector(ConnectorConfig(provider="google_analytics", options=options))


async def test_needs_connect() -> None:
    connector = GoogleAnalyticsConnector(
        ConnectorConfig(provider="google_analytics", options={"property": "1"})
    )
    with pytest.raises(ConnectionError):
        await connector.run_report(ReportSpec(metrics=["sessions"]))


async def test_connect_requires_auth() -> None:
    connector = GoogleAnalyticsConnector(
        ConnectorConfig(provider="google_analytics", options={"property": "1"})
    )
    with pytest.raises(ConfigurationError, match="google_analytics connector requires auth"):
        await connector.connect()


async def test_connect_uses_analytics_scope() -> None:
    connector = GoogleAnalyticsConnector(
        ConnectorConfig(
            provider="google_analytics",
            auth=AuthConfig(
                type="oauth",
                credentials={"client_id": "i", "client_secret": "s", "refresh_token": "r"},
            ),
            options={"property": "1"},
        )
    )
    await connector.connect()
    await connector.close()


async def test_validate() -> None:
    assert await make(FakeAnalytics([])).validate() is True


async def test_validate_false_when_refused() -> None:
    connector = make(FakeAnalytics([]))
    connector._client = GoogleAnalyticsClient(
        FakeCredential(),
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403))),
    )
    assert await connector.validate() is False


async def test_run_report_reads_every_page() -> None:
    fake = FakeAnalytics(ROWS)
    result = await make(fake, page_size=2).run_report(ReportSpec(metrics=["sessions"]))

    assert result.headers == ["country", "sessions"]
    assert result.metric_types == ["TYPE_INTEGER"]
    assert result.rows == [list(r) for r in ROWS]
    assert result.row_count == 3
    assert [(r["offset"], r["limit"]) for r in fake.requests] == [(0, 2), (2, 2)]


async def test_run_report_limit_caps_rows_and_request_size() -> None:
    fake = FakeAnalytics(ROWS)
    result = await make(fake, page_size=2).run_report({"metrics": ["sessions"], "limit": 3})

    assert len(result.rows) == 3
    assert [(r["offset"], r["limit"]) for r in fake.requests] == [(0, 2), (2, 1)]


async def test_run_report_sends_the_spec() -> None:
    fake = FakeAnalytics(ROWS)
    flt = {"filter": {"fieldName": "country", "stringFilter": {"value": "Romania"}}}
    await make(fake).run_report(
        {
            "dimensions": ["country"],
            "metrics": ["sessions"],
            "date_ranges": [{"start_date": "2026-01-01", "end_date": "2026-01-31"}],
            "dimension_filter": flt,
            "order_bys": [{"metric": {"metricName": "sessions"}, "desc": True}],
        }
    )
    body = fake.requests[0]
    assert body["dimensions"] == [{"name": "country"}]
    assert body["dateRanges"] == [{"startDate": "2026-01-01", "endDate": "2026-01-31"}]
    assert body["dimensionFilter"] == flt
    assert body["orderBys"][0]["desc"] is True
    assert "metricFilter" not in body


async def test_run_report_empty_table() -> None:
    result = await make(FakeAnalytics([])).run_report(ReportSpec(metrics=["sessions"]))
    assert result.rows == []
    assert result.headers == ["country", "sessions"]


def test_spec_validation() -> None:
    with pytest.raises(ValueError):
        ReportSpec(metrics=[])
    with pytest.raises(ValueError):
        ReportSpec(
            metrics=["sessions"],
            date_ranges=[DateRange(start_date="today", end_date="today")] * 5,
        )
    assert ReportSpec(metrics=["sessions"]).date_ranges[0].start_date == "28daysAgo"


async def test_list_exposes_reports_as_csv_files() -> None:
    connector = make(FakeAnalytics([]))
    items = [item async for item in connector.list()]

    assert [i.name for i in items] == ["sessions-by-country.csv"]
    assert items[0].type is ItemType.FILE
    assert items[0].mime_type == "text/csv"
    assert items[0].metadata["property"] == PROPERTY
    assert items[0].metadata["metrics"] == ["sessions"]
    assert await connector.get_metadata("sessions-by-country") == items[0]


async def test_list_is_flat() -> None:
    connector = make(FakeAnalytics([]))
    with pytest.raises(NotFoundError):
        [i async for i in connector.list("sub")]
    with pytest.raises(NotFoundError):
        [i async for i in connector.list("../x")]


async def test_list_page_searches_by_name() -> None:
    connector = make(
        FakeAnalytics([]),
        reports={
            "a-sessions": {"metrics": ["sessions"]},
            "b-users": {"metrics": ["totalUsers"]},
        },
    )
    page = await connector.list_page(search="USERS")
    assert [i.id for i in page.items] == ["b-users"]


async def test_get_metadata_unknown_report() -> None:
    with pytest.raises(NotFoundError):
        await make(FakeAnalytics([])).get_metadata("nope")


async def test_download_streams_csv_page_by_page() -> None:
    fake = FakeAnalytics(ROWS)
    connector = make(fake, page_size=2)
    item = await connector.get_metadata("sessions-by-country")

    chunks = [chunk async for chunk in connector.download(item)]

    assert len(chunks) == 2
    assert b"".join(chunks).decode() == (
        'country,sessions\r\nRomania,10\r\nGermany,7\r\n"France, EU",5\r\n'
    )


async def test_download_runs_the_configured_report_not_the_item() -> None:
    fake = FakeAnalytics(ROWS)
    connector = make(fake)
    item = await connector.get_metadata("sessions-by-country")
    forged = item.model_copy(update={"metadata": {"metrics": ["secretMetric"]}})

    [chunk async for chunk in connector.download(forged)]

    assert fake.requests[0]["metrics"] == [{"name": "sessions"}]
    with pytest.raises(NotFoundError):
        [c async for c in connector.download(item.model_copy(update={"id": "other"}))]


async def test_unsupported_capabilities() -> None:
    connector = make(FakeAnalytics([]))
    assert not (connector.supports_sync or connector.supports_write or connector.supports_webhooks)


def client_for(handler: Any) -> GoogleAnalyticsClient:
    return GoogleAnalyticsClient(
        FakeCredential(), http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


async def test_client_error_mapping() -> None:
    for status, error in [(401, AuthenticationError), (404, NotFoundError), (400, DataSourceError)]:
        client = client_for(lambda r, s=status: httpx.Response(s, json={"error": "x"}))
        with pytest.raises(error):
            await client.run_report(PROPERTY, {})


async def test_client_retries_throttling(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_: float) -> None:
        pass

    monkeypatch.setattr("data_sources.connectors.google_analytics.client.asyncio.sleep", no_sleep)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429) if calls < 3 else httpx.Response(200, json={"rows": []})

    assert await client_for(handler).run_report(PROPERTY, {}) == {"rows": []}
    assert calls == 3

    always = client_for(lambda r: httpx.Response(429))
    with pytest.raises(RateLimitError):
        await always.run_report(PROPERTY, {})


def make_without_property(fake: FakeAnalytics) -> GoogleAnalyticsConnector:
    connector = GoogleAnalyticsConnector(ConnectorConfig(provider="google_analytics", options={}))
    connector._client = GoogleAnalyticsClient(
        FakeCredential(), http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    )
    return connector


async def test_list_properties_reads_every_page() -> None:
    connector = make_without_property(FakeAnalytics([]))
    found = await connector.list_properties()

    assert [(p.property, p.display_name, p.account_name) for p in found] == [
        ("properties/11", "Alpha web", "Alpha"),
        ("properties/12", "Alpha app", "Alpha"),
        ("properties/22", "Beta site", "Beta"),
    ]
    assert found[2].account == "accounts/2"


async def test_without_a_property_only_discovery_works() -> None:
    connector = make_without_property(FakeAnalytics([]))

    assert await connector.validate() is True  # lists accounts instead of reading a property
    with pytest.raises(ConfigurationError, match="list_properties"):
        await connector.run_report(ReportSpec(metrics=["sessions"]))
    assert [i async for i in connector.list()] == []  # no reports configured, nothing to ask
