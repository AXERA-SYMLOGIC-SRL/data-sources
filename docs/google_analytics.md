# Google Analytics connector

`GoogleAnalyticsConnector` (`provider = "google_analytics"`) reads one Google Analytics 4
property through the Data API. Analytics holds report tables rather than files, so the
connector offers two ways in:

- **`run_report(spec)`** runs any report on demand and returns its rows.
- **Named reports** (`options.reports`) appear as CSV files: `list()` yields them and
  `download()` runs the report and streams the table.

| Capability | Supported |
|---|:---:|
| `supports_sync` / `supports_item_lookup` / `supports_webhooks` / `supports_permissions` | ❌ |
| `supports_write` | ❌ (read-only) |
| `list_page` (searchable by report name) | ✅ |

Only GA4 is supported; Universal Analytics was shut down by Google.

## Requirements

**In Google Cloud** (the project that owns the credentials):

- Enable the **Google Analytics Data API** (`analyticsdata.googleapis.com`), which runs reports.
- Enable the **Google Analytics Admin API** (`analyticsadmin.googleapis.com`), which
  `list_properties()` needs. Without it only the property list fails; reports still work.
- Not needed: the older "Google Analytics API" and "Analytics Reporting API". They belong to
  Universal Analytics, which no longer exists.

**For `type="oauth"`:**

- An OAuth client of type **Web application**, with every redirect URI your app uses
  authorised on it (the dev service uses `http://localhost:8000/oauth/callback`).
- The consent must request the scope `https://www.googleapis.com/auth/analytics.readonly`,
  with `access_type=offline` so Google returns a refresh token. Adding `prompt=consent`
  makes it return one even when the account consented before.
- On the OAuth consent screen, add the Google accounts that will connect as **test users**
  while the app is in "Testing" status. Google expires the refresh tokens of apps in
  Testing after 7 days; publish the app (Google may require verification for this scope)
  for tokens that last.
- Running the consent flow (authorize URL, redirect, code exchange) is the caller's job;
  `dev/` has a working one to copy.

**For `type="service_account"`:** a key for a service account in that project, and its email
added as a **Viewer** on the GA4 property (Admin → Property access management).

**On the Google side:** the account that connects, or the service account, must have access to
the GA4 property. Properties it can't see don't appear in `list_properties()`.

**On this side:** no extra Python dependencies beyond the base install (it reuses
`google-auth` and `httpx`), and no store: the connector keeps no state.

## Configuring it

```python
from data_sources import ConnectorConfig
from data_sources.config import AuthConfig

config = ConnectorConfig(
    provider="google_analytics",
    auth=AuthConfig(
        type="oauth",
        credentials={"client_id": "...", "client_secret": "...", "refresh_token": "..."},
    ),
    options={
        "property": "123456789",  # or "properties/123456789"
        "reports": {
            "sessions-by-country": {
                "dimensions": ["country"],
                "metrics": ["sessions", "totalUsers"],
                "date_ranges": [{"start_date": "30daysAgo", "end_date": "yesterday"}],
            },
        },
        "page_size": 100000,  # optional, rows per request (max 250000)
    },
)
```

- **`options.property`** is the numeric GA4 property id (Admin → Property details). A
  Measurement ID (`G-XXXX`) or a data stream id is not a property and is refused with
  `ConfigurationError`. It can be left out to connect first and choose after, see
  [Finding the property](#finding-the-property).
- **`options.reports`** is optional. Each entry is a report definition (below); an invalid one
  fails at construction, naming the report.
- **`options.page_size`** is how many rows each request asks for.

### Auth

Same two flows as [Google Drive](google_drive.md#auth), asking for
`https://www.googleapis.com/auth/analytics.readonly`:

- **`type="oauth"`**: a user's refresh token (`client_id`, `client_secret`, `refresh_token`),
  consented with the `analytics.readonly` scope. Acts as that user.
- **`type="service_account"`**: `service_account_info` (the key's JSON), optional `scopes`.
  See [Requirements](#requirements) for the property access it needs.

## Finding the property

`list_properties()` returns every property the credential can see (`PropertySummary`:
`property`, `display_name`, `account`, `account_name`), so a "Connect Google" flow can offer a
picker instead of asking for an id. It uses the Admin API, covered by the same
`analytics.readonly` scope, but the Google Cloud project must have the **Google Analytics Admin
API** enabled as well as the Data API.

```python
connector = create_connector(ConnectorConfig(provider="google_analytics", auth=auth))
await connector.connect()
for p in await connector.list_properties():
    print(p.property, p.display_name, p.account_name)
```

Without `options.property`, only `validate()` (which lists accounts instead) and
`list_properties()` work; anything that reads a property raises `ConfigurationError`.

## Report definitions

A report is a `ReportSpec`:

| Field | Meaning |
|---|---|
| `dimensions` | What to break down by, e.g. `["country", "date"]` (may be empty) |
| `metrics` | The numbers, e.g. `["sessions"]` (at least one) |
| `date_ranges` | 1–4 of `{start_date, end_date}`; `YYYY-MM-DD`, `today`, `yesterday`, `NdaysAgo`. Default: last 28 days. With several, the result gains a `dateRange` column |
| `dimension_filter`, `metric_filter` | A Data API `FilterExpression`, passed through as camelCase JSON |
| `order_bys` | Data API `OrderBy` entries, passed through |
| `limit` | Cap on total rows returned; without it every row is read, page after page |

Names are the API names, listed in the
[dimensions & metrics reference](https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema).
An unknown name makes the API answer 400, which surfaces as `DataSourceError` with Google's message.

## Reading data

```python
async with create_connector(config) as connector:
    result = await connector.run_report(
        {"dimensions": ["country"], "metrics": ["sessions"], "limit": 10}
    )
    result.headers      # ["country", "sessions"]
    result.rows         # [["Romania", "1042"], ...]  every cell is the API's string
    result.metric_types # ["TYPE_INTEGER"]
    result.row_count    # rows the query matches in total, may exceed len(rows) with `limit`

    async for item in connector.list():       # the named reports
        content = b"".join([c async for c in connector.download(item)])  # CSV
```

`download` runs the configured report named by `item.id` (never the definition carried on
the `Item`), so a caller can't use it to run an arbitrary query. The listing is flat: any path
other than the root raises `NotFoundError`.

## Quotas and errors

The Data API throttles per property. A 429 (which is also how an exhausted quota shows) or a
5xx is retried with backoff, honouring `Retry-After`, then raises `RateLimitError`. 401/403 raise
`AuthenticationError`, 404 `NotFoundError`. `validate()` reads the property's metadata and
returns `False` when that fails.
