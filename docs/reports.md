# Report sources (`data_sources.reports`)

Clients for ads and analytics APIs that return each report **exactly as the provider sent it**,
for an application that stores raw records and builds its own tables from them. They complement
the [Google Analytics](google_analytics.md) and [Meta Ads](meta_ads.md) connectors, which turn
reports into CSV tables; a report source holds no configuration of what to read and has no
store, sync or file view.

Every provider is a **report source**: a `ReportSource` subclass in a module under
`data_sources/reports/sources/`. The registry walks that package and keeps every concrete
subclass by its `provider`, so **adding a provider is dropping in a file**; nothing is registered
by hand. Two sources claiming one provider, or a module that fails to import, raise when the
registry first loads. An application builds a source from a `ConnectorConfig`:

| `provider` | Class (module under `sources/`) | API | `auth.credentials` | `options` |
|---|---|---|---|---|
| `google_analytics` | `GoogleAnalyticsReports` (`google_analytics`) | GA4 Data + Admin API | `client_id`, `client_secret`, `redirect_uri` | |
| `google_search_console` | `SearchConsoleReports` (`search_console`) | Search Console API | same | |
| `google_ads` | `GoogleAdsReports` (`google_ads`) | Google Ads REST (`googleAds:search`) | same + `developer_token` (for reads) | `api_version` (`v26`) |
| `meta_ads` | `MetaAdsReports` (`meta_ads`) | Graph API | `app_id`, `app_secret`, `redirect_uri` | `api_version` (`v26.0`) |
| `tiktok_ads` | `TikTokAdsReports` (`tiktok_ads`) | Marketing API v1.3 | `app_id`, `app_secret`, `redirect_uri` | |

The credentials are the **application's** (its OAuth client); what a user's consent buys comes
back as a `Grant` and is passed to every later call.

```python
from datetime import date

from data_sources.config.schema import AuthConfig, ConnectorConfig
from data_sources.reports import create_report_source

source = create_report_source(
    ConnectorConfig(
        provider="google_analytics",
        auth=AuthConfig(
            type="oauth2",
            credentials={
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": "https://app.example.com/oauth/google/callback",
            },
        ),
    )
)
source.configured                        # every app credential it needs is set
url = source.authorize_url(state)        # send the user here to consent
grant = await source.exchange_code(code)  # Grant(credentials={"refresh_token": ...}, identity=email)
accounts = await source.list_accounts(grant.credentials)  # [Account(id, name, group, ...)]
report = await source.read(
    grant.credentials,
    "properties/123456789",
    {"dimensions": ["date", "country"], "metrics": ["sessions"]},
    since=date(2026, 10, 1),
    until=date(2026, 10, 7),
)
```

### The interface

Every source implements `authorize_url(state)`, `exchange_code(code) -> Grant`,
`list_accounts(credentials) -> [Account]` and `read(credentials, account, spec, since, until)
-> RawReport`. What differs is the shape of a grant, an account and a report spec:

| `provider` | `Grant.credentials` (`identity`, `expires_in`) | `Account.id` | `spec` |
|---|---|---|---|
| `google_analytics` | `refresh_token` (email) | `properties/{id}` (`group`: GA account) | `dimensions`, `metrics`, `day_column`? (`date`) |
| `google_search_console` | `refresh_token` (email) | site URL | `dimensions` (first is `date`) |
| `google_ads` | `refresh_token` (email) | `customer`, or `customer:manager` for a customer reached through a manager (`group`) | `resource`, `fields` |
| `meta_ads` | `access_token` (the user's name, ~60 days) | `act_{id}` | `fields`, `level`, `breakdowns`?, `time_increment`?, `day_column`? |
| `tiktok_ads` | `access_token`, `advertiser_ids` | advertiser id (one of those) | `data_level`, `dimensions`, `metrics` |

A source keeps its provider's own calls too, for what doesn't fit the interface: GA's
`list_fields` (a property's dimensions and metrics, custom ones included), Meta's
`list_custom_conversions` and `user_name`, and the typed `list_properties`, `list_customers`,
`list_sites`, `list_ad_accounts`, `list_advertisers`, `read_report` and `read_insights`.

The three Google sources share `GoogleReportSource` (`reports/google.py`): one OAuth web client,
each consent asking for its own scope (`SCOPE` in each module), so a user grants Analytics, Ads
and Search Console separately. It is abstract (no `provider`) and lives outside `sources/`.

### Adding a provider

1. A module `data_sources/reports/sources/<provider>.py` with a `ReportSource` subclass: its
   `provider`, `app_credentials`, and the four methods. Shared code for several sources goes
   outside `sources/` (like `google.py`).
2. Its requests through `reports.http.ApiClient`, with the provider's quirks in a `classify`
   function (see below); paging helpers are in `reports.paging`, date windows in
   `reports.windows`.
3. Tests on an `httpx.MockTransport` (`tests/test_reports_*.py`), and a row in the tables above.

## Shape

Every read returns a `RawReport`:

- `records`: the API's own rows, untouched (GA's `{"dimensionValues", "metricValues"}`, Meta's
  insight objects, TikTok's report items, Google Ads' `results`, Search Console's `rows`).
- `days`: the day of each record, parallel to it (`None` for a report with no daily split), so a
  caller can replace exactly the days it read.
- `envelope`: what describes the rows. For GA it is the first response without its rows
  (`dimensionHeaders`, `metricHeaders`, `metadata`, `rowCount`, ...): GA's rows are positional, so
  keep the headers with them. For the others it names what was read (site, dimensions, customer,
  advertiser).

## Errors

Everything a provider refuses raises `ReportError` with a `reason`; `ReportAuthError` (also an
`AuthenticationError`) when the grant is the problem and the user has to authorize again:

| `reason` | Meaning |
|---|---|
| `unavailable` | network error, rate limit or 5xx after the retries, or an invalid response |
| `auth_failed` | the token is invalid, expired, revoked or missing a permission (`ReportAuthError`) |
| `request_failed` | the provider refused the request itself (TikTok permissions, a bad Google Ads query) |
| `oauth_not_configured` | the OAuth client id or secret is empty |
| `no_refresh_token` | Google's consent returned no refresh token |
| `access_denied` | the consent didn't grant the scope asked for |
| `customer_not_enabled` | a Google Ads customer is disabled (skipped when listing customers) |

Messages never contain a credential or the provider's response body; Google Ads refusals are
logged (`data-sources.google_ads.client`) with every credential of the request removed.

## Retries and tests

Requests go through `reports.http.ApiClient`: idempotent requests (and report queries sent as
POST) are retried with backoff on rate limits and 5xx, honouring the wait a provider names (GA's
`Retry-After`, Meta's `x-business-use-case-usage`; Meta throttles longer than two minutes fail
instead). Each source classifies its provider's responses (`classify`): TikTok reports errors
inside HTTP 200 answers, Search Console its quota as a 403, Meta its throttles as Graph error
codes.

Every source takes `transport` (an `httpx` transport, e.g. `httpx.MockTransport`) and `sleep`
(what it waits with between retries), so tests run without the network or real waits.
