from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class DateRange(BaseModel):
    """A date range as the Data API takes it: `YYYY-MM-DD`, or `today`, `yesterday`,
    `NdaysAgo`."""

    start_date: str
    end_date: str


def _default_ranges() -> list[DateRange]:
    return [DateRange(start_date="28daysAgo", end_date="yesterday")]


class ReportSpec(BaseModel):
    """One Data API report: what to break the numbers down by, which numbers, and when.

    `dimension_filter`, `metric_filter` and `order_bys` are passed to the API as they are
    (camelCase `FilterExpression` / `OrderBy` JSON), so everything the API can filter or
    sort on works without this model re-describing it. `limit` caps the rows returned in
    total; without it every row is read, page after page.
    """

    dimensions: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(min_length=1)
    date_ranges: list[DateRange] = Field(default_factory=_default_ranges)
    dimension_filter: dict[str, Any] | None = None
    metric_filter: dict[str, Any] | None = None
    order_bys: list[dict[str, Any]] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1)

    @field_validator("date_ranges")
    @classmethod
    def _at_most_four_ranges(cls, ranges: list[DateRange]) -> list[DateRange]:
        # The API refuses more than four; failing here names the cause.
        if not 1 <= len(ranges) <= 4:
            raise ValueError("a report takes between 1 and 4 date ranges")
        return ranges

    def request_body(self, *, offset: int, page_size: int) -> dict[str, Any]:
        """The `runReport` body for the page of up to `page_size` rows starting at `offset`."""
        body: dict[str, Any] = {
            "dimensions": [{"name": name} for name in self.dimensions],
            "metrics": [{"name": name} for name in self.metrics],
            "dateRanges": [
                {"startDate": r.start_date, "endDate": r.end_date} for r in self.date_ranges
            ],
            "limit": page_size,
            "offset": offset,
        }
        if self.dimension_filter:
            body["dimensionFilter"] = self.dimension_filter
        if self.metric_filter:
            body["metricFilter"] = self.metric_filter
        if self.order_bys:
            body["orderBys"] = self.order_bys
        return body


class ReportResult(BaseModel):
    """What a report returned. `headers` is the dimensions then the metrics, as the API
    reports them (a report over several date ranges gains a `dateRange` dimension).
    Every cell is the API's string; `metric_types` (parallel to the metric columns, e.g.
    `TYPE_INTEGER`) says how to read the numbers. `row_count` is the total the query
    matches, which exceeds `len(rows)` when `limit` cut it short."""

    headers: list[str]
    metric_types: list[str]
    rows: list[list[str]]
    row_count: int


class PropertySummary(BaseModel):
    """A GA4 property the credential can see, with the account it belongs to."""

    property: str  # `properties/{id}`, what `options.property` takes
    display_name: str
    account: str  # `accounts/{id}`
    account_name: str
