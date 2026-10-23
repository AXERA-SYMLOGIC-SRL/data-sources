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

## Environment variables

The connector itself reads **no** environment variables: everything comes in through
`ConnectorConfig`, and the YAML loader doesn't expand `${VAR}`. Reading the environment is
your application's job. These are the names the [dev service](#the-dev-service) uses, a good
convention to follow:

| Variable | Goes into | Needed for |
|---|---|---|
| `META_APP_ID` | `oauth.authorize_url`, `exchange_code`, `extend_token` | The consent flow |
| `META_APP_SECRET` | `auth.credentials.app_secret`, and `exchange_code` / `extend_token` | The consent flow; recommended on every call (adds `appsecret_proof`) |
| `META_REDIRECT_URI` | `oauth.authorize_url` and `exchange_code` | The consent flow: must match a valid OAuth redirect URI of the app exactly |
| `META_ACCESS_TOKEN` | `auth.credentials.access_token` | Only with a system-user token, which is one value for the whole app |

A user's long-lived token from the consent flow is per user and expires in about 60 days, so it
belongs in your database with its expiry, not in an environment variable. Treat the app secret
and every token as secrets.

```python
import os

auth = AuthConfig(
    type="access_token",
    credentials={
        "access_token": os.environ["META_ACCESS_TOKEN"],  # system-user token
        "app_secret": os.environ["META_APP_SECRET"],
    },
)
```

### Where to get them

Meta renames menus often; if a label differs, the steps in
[Meta's docs](https://developers.facebook.com/docs/marketing-api/overview/authorization) apply.

**`META_APP_ID` and `META_APP_SECRET`**

1. In [Meta for Developers](https://developers.facebook.com/apps/), **Create App**, type
   **Business**, then add the **Marketing API** and **Facebook Login** products
   (see [Requirements](#requirements)).
2. **App settings → Basic**: the **App ID** is at the top, and the **App Secret** is behind
   **Show** (Meta asks for your Facebook password).

**`META_REDIRECT_URI`** is not issued: you pick it. It is the URL of your callback route, for
the dev service `http://localhost:8000/meta/oauth/callback`. Add it under **Facebook Login →
Settings → Valid OAuth Redirect URIs**; it must match what you send exactly or the consent
fails.

**`META_ACCESS_TOKEN`** (a system-user token, instead of a user's consent)

1. In [Business Settings](https://business.facebook.com/settings), **Users → System users →
   Add**. An admin of the business has to do this, and the app must belong to (or be connected
   to) that business.
2. Select the system user → **Add assets**: give it the ad account(s) with at least
   view-performance access.
3. **Generate new token**: pick the app, choose the expiry (Meta recommends tokens that
   expire; a never-expiring one is a standing credential), tick **`ads_read`** (plus
   `business_management` if you list portfolios), and copy the token. It is shown once.

**The ad account id** (`options.ad_account`) is in Ads Manager's account dropdown, a number you
can write with or without `act_`. Or skip it and use
[`list_ad_accounts()`](#finding-the-ad-account).

**A user's long-lived token** comes from the [consent flow](#the-consent-flow), not from a
console.

### The dev service

`dev/` (local only, not in git) also needs:

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | none, required | SQLAlchemy async URL, e.g. `postgresql+asyncpg://dev:dev@localhost:5433/data_sources` |
| `META_APP_ID`, `META_APP_SECRET` | none, required for Meta | As above |
| `META_REDIRECT_URI` | `http://localhost:8000/meta/oauth/callback` | As above |

They go in `dev/.env` (copy `dev/.env.example`), which the service loads on start. The Meta
variables are only needed once you use the Meta section of the page.

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
