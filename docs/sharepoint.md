# SharePoint connector

`SharePointConnector` (`provider = "sharepoint"`) connects to a single SharePoint/OneDrive
document library — one drive, optionally scoped to a subfolder within it. To cover a second
library, configure a second connector instance.

| Capability | Supported |
|---|:---:|
| `supports_sync` (incremental, via Graph delta) | ✅ |
| `supports_item_lookup` (`get_item` without a live call) | ✅ |
| `supports_webhooks` (Graph change notifications) | ✅ |
| `supports_permissions` | ❌ |

## Configuring it

```python
from data_sources import ConnectorConfig
from data_sources.config import AuthConfig

config = ConnectorConfig(
    provider="sharepoint",
    name="finance-q1",  # see "Why set `name`" below
    auth=AuthConfig(
        type="client_credentials",
        credentials={
            "tenant_id": "...",
            "client_id": "...",
            "client_secret": "...",
        },
    ),
    options={
        "url": "https://contoso.sharepoint.com/sites/Finance/_layouts/15/AllItems.aspx"
        "?id=/sites/Finance/Shared Documents/Reports/Q1",
        "excluded_paths": ["Archive"],  # optional, relative to the folder above
    },
)
```

- **`auth.credentials`** — an Azure AD app registration with `Sites.Read.All` (or
  narrower, site-specific) Graph application permissions. All three of `tenant_id`,
  `client_id`, `client_secret` are required; the connector raises `ConfigurationError`
  at `connect()` time if any are missing.

  Prefer `Sites.Selected` over `Sites.Read.All` where possible — see "Narrowing access
  with `Sites.Selected`" below. It's the same `client_credentials` auth either way;
  only the app registration's Graph permission and a one-time per-site grant differ.
- **`options.url`** — a SharePoint *sharing link* (the kind you get from the "Copy link"
  button), not a REST/Graph URL. The connector parses it with
  `SharepointClient.parse_sharing_url` to recover the drive's display name and the
  folder path within it; everything under that folder is in scope.
- **`options.excluded_paths`** — path prefixes, relative to the folder above, to skip
  during `list()` and `sync()` (e.g. `["Archive"]` skips `Reports/Q1/Archive/...`).

### Why set `name`

The connector persists its sync cursor, item index, and webhook subscription state
keyed by `config.name` if set, falling back to the raw sharing URL otherwise (see
`_sync_key`). Set `name` explicitly and keep it stable — if the sharing URL ever changes
(a folder gets renamed, the link gets regenerated) while `name` doesn't, the connector
keeps resuming from the same state instead of silently starting a fresh sync under a new key.

## Narrowing access with `Sites.Selected`

`Sites.Read.All` (application permission) grants the app read access to *every*
SharePoint site in the tenant, and needs a Global/SharePoint admin to consent to it
once, up front. `Sites.Selected` is a narrower alternative: on its own it grants the
app access to **no** sites — each site must be individually authorized via a separate
Graph call, made once per site:

```python
POST https://graph.microsoft.com/v1.0/sites/{site-id}/permissions
{
  "roles": ["read"],
  "grantedToIdentities": [{"application": {"id": "<app-client-id>", "displayName": "..."}}]
}
```

`SharepointClient.grant_site_permission(site_id, app_client_id, roles)` wraps this
call. The catch: it must be made with a **delegated** credential (from an interactive
sign-in by that site's owner, or a SharePoint admin) — not the connector's own
`ClientSecretCredential`, which has no standing to grant permissions on a site it can't
access yet. In practice this means constructing a second, throwaway `SharepointClient`
around a credential object wrapping the delegated access token, using it for this one
call, then discarding it — the app's regular `client_credentials` config (`tenant_id`/
`client_id`/`client_secret`) is untouched and keeps being used for every other call
this connector makes.

This library only exposes the primitive (`grant_site_permission`); driving the actual
one-time consent flow — building the Microsoft authorize URL, handling the redirect
callback, exchanging the code for a delegated token — is left to the caller, since it
usually needs to be wired into whatever UI lets an admin manage connector configs (e.g.
an "Authorize site" button). `Sites.FullControl.All` (delegated) is the permission
believed to be required to call the grant endpoint itself — confirm against current
Microsoft Graph docs, since Microsoft has moved this requirement before.

Once granted, `Sites.Read.All`/`Sites.ReadWrite.All` can be removed from the app
registration entirely — `Sites.Selected` plus the per-site grants above are all this
connector needs going forward. A site that was never granted, or whose grant was
revoked, simply 403s on `connect()`/`validate()`, surfaced as
`AuthenticationError`/`ConfigurationError` like any other credential problem.

## Using it

A `SharePointConnector` needs a `Store` to back `sync`, item lookup, and webhooks — those
methods read/write `self.store`, set by `init_connector`, not by the constructor. Read-only
use (`list`, `get_metadata`, `download`) works without one.

```python
from data_sources.store import init_store, init_connector

store = await init_store()  # see store.md — defaults to a local SQLite file
connector = await init_connector(config, store)  # creates its tables, sets connector.store

await connector.connect()  # acquires the Graph credential + resolves site/drive ids
await connector.validate()  # True if the configured folder is reachable

async for item in connector.list(recursive=True):
    print(item.name)

await connector.close()
```

### Incremental sync

```python
from data_sources.core import ChangeType


async def on_change(change):
    if change.type is ChangeType.DELETED:
        ...
    else:
        content = b"".join([chunk async for chunk in connector.download(change.item)])


await connector.sync_in_background(on_change)
```

`sync_in_background` loads the connector's own persisted cursor, walks Graph's delta feed
via `sync()`, calls `on_change` per change, then commits that change's cursor — so a crash
between the two redelivers that one change next run; `on_change` must tolerate that.
It also updates the connector's own item index as it goes, so a later call elsewhere with
just an id — `await connector.get_item(item_id)` — can resolve the `Item` without hitting
Graph again.

`sync_in_background` runs until the delta feed is exhausted, once. Call it again — on a
timer, or in reaction to a webhook — to keep picking up new changes; it does not loop or
poll on its own.

## Webhooks

Graph webhooks (subscriptions) are pings, not payloads: a notification tells you *that*
something changed under a resource, never *what* — you still have to call `sync()` to find
out. Subscriptions also expire (30 days max for this resource type) and must be renewed.

```python
subscription = await connector.create_webhook("https://your-app.example.com/sharepoint/webhooks")
# subscription.id, .expiration — renew before it lapses:
await connector.renew_webhook(subscription.id)
# ... and eventually:
await connector.delete_webhook(subscription.id)
```

`create_webhook` generates a random `client_state` secret, sends it to Graph, and persists
it via the store. Graph validates `notification_url` synchronously as part of that call — it
POSTs a `validationToken` there and needs it echoed back as plain text within 10 seconds —
so the receiving endpoint (below) must already be reachable and running *before* you call
`create_webhook`.

### Wiring notifications into an HTTP endpoint

Mount the connector via `data_sources.web`, which handles the validation handshake and
signature verification for you (`connector.verify_webhook_notification` checks the inbound
`subscriptionId`/`clientState` against what `create_webhook` persisted):

```python
from fastapi import FastAPI
from data_sources.web import build_connectors_router


async def on_webhook_notification(connector, payload):
    # "something changed" — decide *if and when* to actually sync. This connector's
    # notifications never carry data of their own, so `payload` isn't useful here
    # (unlike providers whose webhook body *is* the event).
    await connector.sync_in_background(on_change)


app = FastAPI()
app.include_router(
    build_connectors_router(
        {"finance-q1": connector}, on_webhook_notification=on_webhook_notification
    ),
    prefix="/sharepoint",
)
```

Two things this router deliberately leaves to you:

- **It never calls `sync()` itself.** It only verifies the notification and awaits your
  callback — what runs, and when, is entirely up to the caller.
- **It doesn't protect against overlapping runs.** If you also poll this connector on a
  timer, a webhook notification can arrive mid-poll and trigger a second, concurrent
  `sync_in_background` call — both would read/commit the same persisted cursor. Guard
  against that yourself, e.g. an `asyncio.Lock` per connector that a poll loop and the
  webhook callback both acquire before calling `sync_in_background`:

  ```python
  import asyncio

  sync_lock = asyncio.Lock()


  async def run_sync():
      if sync_lock.locked():
          return  # a run is already in flight; it'll pick up this change too
      async with sync_lock:
          await connector.sync_in_background(on_change)
  ```
