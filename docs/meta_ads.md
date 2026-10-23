# Meta Ads connector

`MetaAdsConnector` (`provider = "meta_ads"`) reads one Meta (Facebook / Instagram) ad account
through the Marketing API's Insights. Like [Google Analytics](google_analytics.md), ads data is
report tables rather than files, so there are two ways in:

- **`run_insights(spec)`** runs any query on demand and returns its rows.
- **Named reports** (`options.reports`) appear as CSV files: `list()` yields them and
  `download()` runs the query and streams the table.

| Capability | Supported |
|---|:---:|
| `supports_sync` / `supports_item_lookup` / `supports_webhooks` / `supports_permissions` | ❌ |
| `supports_write` | ❌ (read-only) |
| `list_page` (searchable by report name) | ✅ |

## Requirements

**In Meta for Developers:**

- An app (type **Business**) with the **Marketing API** product added.
- For a "Connect with Facebook" flow, the **Facebook Login** product, with your redirect URI
  (the dev service uses `http://localhost:8000/meta/oauth/callback`) listed under its
  valid OAuth redirect URIs.
- The permission **`ads_read`**, plus **`business_management`** to list business portfolios. While the app is in Development mode it works for ad accounts
  of people who have a role on the app. To read other people's ad accounts the app needs
  **Advanced Access** to `ads_read`, which requires App Review and Business Verification.
  Check Meta's current docs for what your case needs.

**A token**, one of:

- A **system-user token** (Business Settings → System users → generate token with `ads_read`),
  the usual choice for a server.
- A **long-lived user token** from the consent flow, about 60 days.

**Meta has no refresh tokens.** A user token cannot be renewed in the background: when it
expires the user consents again, and the connector raises `AuthenticationError` meanwhile.
Plan for that, or use a system-user token.

**On this side:** no extra Python dependencies, and no store (the connector keeps no state).

## Configuring it

```python
from data_sources import ConnectorConfig
from data_sources.config import AuthConfig

config = ConnectorConfig(
    provider="meta_ads",
    auth=AuthConfig(
        type="access_token",
        credentials={
            "access_token": "...",
            "app_secret": "...",  # optional: adds appsecret_proof to every call
        },
    ),
    options={
        "ad_account": "act_123456789",  # or "123456789"
        "api_version": "v26.0",         # optional, the default
        "reports": {
            "spend-by-campaign": {
                "fields": ["campaign_name", "impressions", "clicks", "spend"],
                "level": "campaign",
                "date_preset": "last_30d",
            },
        },
        "page_size": 500,  # optional, rows per request (max 5000)
    },
)
```

- **`options.ad_account`** is optional: leave it out to connect first and choose after, see
  [Finding the ad account](#finding-the-ad-account).
- **`options.api_version`** pins the Graph API version. Meta retires old versions on a
  schedule, so upgrade it deliberately.
- **`app_secret`** is recommended where the app has "Require App Secret" on.

## Finding the ad account

`list_ad_accounts()` returns every ad account the token can see (`AdAccountSummary`: `account`,
`name`, `currency`, `status`, `timezone`), so a connect flow can offer a picker. `status` is
Meta's `account_status` code; `1` is active. Without `options.ad_account`, only `validate()`
and the listings below work.

To group them by business portfolio:

```python
for business in await connector.list_businesses():          # BusinessSummary: id, name
    for account in await connector.list_ad_accounts(business.id):
        print(business.name, account.account, account.relationship)  # "owned" or "client"
```

`owned` accounts belong to the portfolio; `client` ones are shared in by another business.
An account listed under both appears once, as `owned`. Both calls need the
**`business_management`** permission in addition to `ads_read` (ask for both in `authorize_url(..., scopes=("ads_read", "business_management"))`); without it they raise
`AuthenticationError` naming it.

## The consent flow

`data_sources.connectors.meta_ads.oauth` has the Facebook Login steps:

```python
from data_sources.connectors.meta_ads.oauth import authorize_url, exchange_code, extend_token

url = authorize_url(app_id, redirect_uri, state)            # send the user here
short = await exchange_code(http, app_id, app_secret, redirect_uri, code)  # ~1 hour
long = await extend_token(http, app_id, app_secret, short.token)           # ~60 days
# long.token goes in auth.credentials.access_token; long.expires_in says when it ends
```

Running the redirect and checking `state` is the caller's job; `dev/` has a working one.

## Insights queries

An `InsightsSpec`:

| Field | Meaning |
|---|---|
| `fields` | What to return, e.g. `["campaign_name", "impressions", "spend", "actions"]` (at least one) |
| `level` | `account` (default), `campaign`, `adset` or `ad` |
| `date_preset` | `last_7d`, `last_30d` (default), `this_month`, `maximum`… |
| `since` / `until` | An explicit `YYYY-MM-DD` period instead of a preset; give both |
| `time_increment` | `1`–`90` days, `"monthly"`, or `"all_days"` (one row for the period) |
| `breakdowns` | e.g. `["age", "gender"]`; not every combination is allowed |
| `filtering`, `sort` | Passed through as the API takes them |
| `limit` | Cap on total rows; without it every row is read |

```python
async with create_connector(config) as connector:
    result = await connector.run_insights(
        {"fields": ["campaign_name", "spend"], "level": "campaign", "limit": 20}
    )
    result.headers  # the requested fields, then what the API added (date_start, date_stop…)
    result.rows     # every cell is the API's string
```

A nested value such as `actions` (a list of `{action_type, value}`) is kept as compact JSON in
its cell. Queries run synchronously; for a very large range Meta may time out, so narrow the
period or the level. An unknown field or a bad combination is a 400, raised as
`DataSourceError` with Meta's message.

`download` runs the configured report named by `item.id` (never the definition on the `Item`).
CSV columns are fixed by the first page: the requested fields plus the extra columns that page
carried. The listing is flat; a path other than the root raises `NotFoundError`.

## Rate limits and errors

Meta throttles per ad account (Business Use Case limits) and per app. A throttle is retried
with backoff; when Meta reports the account is blocked for more than two minutes
(`x-business-use-case-usage`), `RateLimitError` is raised at once saying how long. An invalid
or expired token, or a missing permission, raises `AuthenticationError`. `validate()` returns
`False` when the check fails.
