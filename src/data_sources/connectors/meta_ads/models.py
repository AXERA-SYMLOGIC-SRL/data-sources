from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

Level = Literal["account", "campaign", "adset", "ad"]


class InsightsSpec(BaseModel):
    """One Insights query on an ad account.

    `fields` are the metrics and identifiers to return (`impressions`, `spend`,
    `campaign_name`, `actions`…). `level` is how finely to split the account. The period is
    either `date_preset` (`last_7d`, `last_30d`, `this_month`, `maximum`…) or an explicit
    `since`/`until` (`YYYY-MM-DD`), defaulting to `last_30d`. `time_increment` splits it by
    `1`..`90` days, `"monthly"` or `"all_days"` (one row for the period). `filtering` and
    `sort` pass through as the API takes them. `limit` caps the rows in total; without
    it every row is read.
    """

    fields: list[str] = Field(min_length=1)
    level: Level = "account"
    date_preset: str | None = None
    since: str | None = None
    until: str | None = None
    time_increment: int | Literal["monthly", "all_days"] | None = None
    breakdowns: list[str] = Field(default_factory=list)
    filtering: list[dict[str, Any]] = Field(default_factory=list)
    sort: list[str] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _one_way_to_say_when(self) -> InsightsSpec:
        if bool(self.since) != bool(self.until):
            raise ValueError("since and until go together")
        if self.since and self.date_preset:
            raise ValueError("give either date_preset or since/until, not both")
        return self

    def request_params(self, *, page_size: int, after: str | None) -> dict[str, Any]:
        """Query parameters for one page of up to `page_size` rows, after cursor `after`."""
        params: dict[str, Any] = {
            "fields": ",".join(self.fields),
            "level": self.level,
            "limit": page_size,
        }
        if self.since and self.until:
            params["time_range"] = _json({"since": self.since, "until": self.until})
        else:
            params["date_preset"] = self.date_preset or "last_30d"
        if self.time_increment is not None:
            params["time_increment"] = self.time_increment
        if self.breakdowns:
            params["breakdowns"] = ",".join(self.breakdowns)
        if self.filtering:
            params["filtering"] = _json(self.filtering)
        if self.sort:
            params["sort"] = ",".join(self.sort)
        if after:
            params["after"] = after
        return params


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


class InsightsResult(BaseModel):
    """What a query returned. `headers` are the requested fields first, then whatever else the
    API added (`date_start`, `date_stop`, breakdown columns). Cells are the API's strings;
    a nested value (`actions`, a list of `{action_type, value}`) is kept as compact JSON."""

    headers: list[str]
    rows: list[list[str]]


class BusinessSummary(BaseModel):
    """A business portfolio (Meta Business) the token's user belongs to."""

    id: str
    name: str


class AdAccountSummary(BaseModel):
    """An ad account the token can see. Listed through a business portfolio it also says
    which one and how the portfolio holds it: `owned` (its own) or `client` (shared in by
    another business)."""

    account: str  # `act_{id}`, what `options.ad_account` takes
    name: str
    currency: str
    status: int  # Meta's `account_status` code; 1 is active
    timezone: str
    business_id: str | None = None
    relationship: Literal["owned", "client"] | None = None
