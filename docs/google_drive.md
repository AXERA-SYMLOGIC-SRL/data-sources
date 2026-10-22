# Google Drive connector

`GoogleDriveConnector` (`provider = "google_drive"`) connects to one Google Drive folder and
everything below it. The folder can be in My Drive, shared with the account, or on a shared
drive. To cover a second folder, configure a second connector instance.

| Capability | Supported |
|---|:---:|
| `supports_sync` (incremental, via Drive's change feed) | ✅ |
| `supports_item_lookup` (`get_item` without a live call) | ✅ |
| `supports_webhooks` (Drive push notification channels) | ✅ |
| `supports_permissions` | ✅ |

## Configuring it

```python
from data_sources import ConnectorConfig
from data_sources.config import AuthConfig

config = ConnectorConfig(
    provider="google_drive",
    name="finance-reports",  # keys the connector's persisted state, as for SharePoint
    auth=AuthConfig(
        type="oauth",
        credentials={
            "client_id": "....apps.googleusercontent.com",
            "client_secret": "...",
            "refresh_token": "...",
        },
    ),
    options={
        "folder": "https://drive.google.com/drive/folders/1AbC...",  # or the bare id, or "root"
        "excluded_paths": ["Archive"],  # optional, relative to the folder above
    },
)
```

- **`options.folder`** is the folder's URL or id, or `root` for the whole of My Drive.
  It's never a folder name: a name doesn't pick one folder in a Drive, so a name is
  refused with `ConfigurationError`. `connect()` resolves it to the real id and notes the
  shared drive it is on, if any.
- **`options.excluded_paths`** lists folders, relative to the folder above, to skip in
  `list()` and `sync()`. Matching is by whole path segment: `Archive` skips
  `Archive/2025/...` but not `Archive2025`.
- **`options.export_formats`** is optional and sets what Google-native files are
  downloaded as. A Google Doc has no bytes of its own, so it is exported. The defaults
  are Docs, Slides and Drawings as PDF, and Sheets as XLSX. Override or add entries per
  Google type:

  ```python
  options = {
      "folder": "...",
      "export_formats": {
          "application/vnd.google-apps.document": {
              "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
              "extension": ".docx",
          },
      },
  }
  ```

  A Google-native type with no export format, such as a Form or a Site, can't be read
  and is not listed. Drive caps exports at 10 MB.

### Auth

**`type="oauth"`** uses a user's consent, kept as its refresh token. This is what a
"Connect Google" button produces. The connector acts as that user, within the scopes they
consented to. `https://www.googleapis.com/auth/drive.readonly` covers reading;
[writing](#writing) needs `https://www.googleapis.com/auth/drive`.
Requires `client_id`, `client_secret` and `refresh_token`. Running the consent flow
(authorize URL, redirect, code exchange) is the caller's job.

**`type="service_account"`** uses a service account's key:

```python
AuthConfig(
    type="service_account",
    credentials={
        "service_account_info": {...},  # the key file's JSON, as a dict or as text
        "subject": "person@company.com",  # optional: domain-wide delegation
        "scopes": ["https://www.googleapis.com/auth/drive.readonly"],  # optional, the default
    },
)
```

Without `subject`, a service account sees only what is shared with its own email address
or the shared drives it is a member of. Share the folder with
`...@....iam.gserviceaccount.com`. With `subject`, it acts as that Workspace user, which a
Workspace admin must allow for the service account's client id (domain-wide delegation).

Either way, a refused or revoked credential raises `AuthenticationError` on the first call.

## The folder is a boundary

`get_metadata`, `download` and `get_permissions` check the item's current ancestry on every
call, whatever id the caller passes. An item whose parents don't lead to the connected
folder raises `NotFoundError`, so nothing about what lies outside is revealed. The same goes
for an item in an excluded folder, a trashed item and a shortcut. `download(item)` checks
`item.id` again rather than trusting the `Item` it is given, since the file may have moved
since the caller kept it. Shortcuts are never followed.

## Using it

As with SharePoint, `sync`, item lookup and webhooks need a `Store`. `list`, `get_metadata`,
`download` and `get_permissions` work without one.

```python
from data_sources.store import init_store, init_connector

store = await init_store()
connector = await init_connector(config, store)

await connector.connect()
await connector.validate()

async for item in connector.list(recursive=True):
    print(item.path, item.name)  # path: the parent folder, relative to the connected one

async for item in connector.list("Q1/Invoices"):  # one level, subfolders included
    ...

await connector.close()
```

`list(path)` resolves `path` one exact folder name at a time under the connected folder.
A missing name raises `NotFoundError` and an ambiguous one (two sibling folders with the
same name) raises `DataSourceError`. `list(recursive=True)` returns files only;
`list(recursive=False)` also returns the subfolders, as `ItemType.FOLDER`.

### Paged listing

For a UI that shows a page at a time:

```python
page = await connector.list_page("Q1", page_size=50, search="invoice")
# page.items, page.next_page_token: pass it back with the same arguments for the next page
```

`search` keeps items whose name contains it, case-insensitively. Paging uses Drive's own
page tokens. A recursive listing carries the folders still to read in the token, and each
of those folders is placed again by its current ancestry before it is read. A folder that
isn't inside the connected folder, whether forged into a token or moved out since, is
skipped rather than listed. A token from another listing (another path, or `recursive`
differing) raises `InvalidPageTokenError`.

## Writing

```python
from data_sources.core import ConflictBehavior

item = await connector.upload("Out/2026", "report.pdf", pdf_bytes)  # folders made as needed
item = await connector.upload("Out/2026", "report.pdf", newer, on_conflict=ConflictBehavior.REPLACE)
await connector.create_folder("Out/2027")
await connector.delete(item.id)  # to Drive's bin; permanent=True deletes outright
```

- Writing needs the `drive` scope. Through a service account, pass it in
  `auth.credentials.scopes`. A token without it gets `AuthenticationError` on the first
  write.
- Drive allows two files with the same name in a folder, so the connector checks for the
  name first. A file **or folder** with the name counts as taken:
  - `FAIL` (the default) raises `ConflictError`.
  - `REPLACE` writes new content over the file, keeping its id and revision history. It
    refuses a folder, a Google-native file (which has no bytes to write over), or two
    files sharing the name.
  - `RENAME` writes beside it as `report (1).pdf`, `report (2).pdf`, and so on.

  Checking and writing aren't one atomic step, so two writers racing can still end up
  with two files of the same name.
- Content over 5 MB goes up in a resumable session, in 8 MB chunks.
- Writes stay inside the connected folder but may go into an excluded one. An output
  folder kept out of what is read is a common destination.
- A file the connector writes shows up in its own `sync()` like any other change.
- `delete` refuses the connected folder itself and anything outside it. A folder is
  deleted together with its contents.

## Incremental sync

The calls are the same as SharePoint's (see [sharepoint.md](sharepoint.md)):
`sync_in_background(on_change)` resumes from the connector's own cursor, calls `on_change`
per change, and commits the cursor after it. `on_change` must tolerate a change delivered
twice.

What's specific to Drive:

- **The first sync walks the folder.** There's no cursor yet, so every file in it comes
  as `CREATED`. The change-feed token is taken *before* the walk, so a change made during
  it is read again next time rather than missed.
- **Later syncs read Drive's change feed.** It covers the whole account (or the whole
  shared drive), not one folder. Each change is placed by its file's current ancestry, and
  changes outside the folder are dropped. A sync can end on pages with nothing inside the
  folder; `sync_in_background` still moves the cursor past them, so a busy account with a
  quiet folder isn't re-read from the same point every time.
- **Deletions come from the item index.** Drive doesn't say where a removed file used to
  be, so a file is reported `DELETED` (trashed, removed, or moved out of the folder) only
  if the index knows it was inside. `CREATED` vs `UPDATED` is likewise decided by whether
  the index already holds the file.
- **Folder moves.** Drive reports a folder change but nothing for the files inside it.
  The connector records where each folder was at the last sync and compares:
  - a folder that **came in** is walked, and its files come as `CREATED`;
  - one that was **moved or renamed** inside is walked again, and its files come as
    `UPDATED` with their new `path`;
  - one that **left** (moved out, trashed, removed) has every indexed file under it
    reported `DELETED`.
  These folder records are committed together with the cursor, never ahead of the
  changes they caused.

Used without a store, `sync()` follows only file changes. It can't tell what used to be
inside, so it reports no deletions and ignores folder moves.

`sync_in_background` runs once, to the end of the change feed. Call it again on a timer, or
when a webhook arrives.

## Webhooks

Drive's push notifications are pings: they say *that* something changed in the account,
never what. You still call `sync()` to find out.

```python
subscription = await connector.create_webhook("https://your-app.example.com/drive/webhooks")
# A channel lives at most 7 days. Renewing opens a new one and stops the old:
subscription = await connector.renew_webhook(subscription.id)  # note: a new id
await connector.delete_webhook(subscription.id)
```

- `create_webhook` opens a channel on the change feed with a random secret token and
  keeps it in the store. Drive has no API that lists channels, so `list_webhooks` reads the
  store, and a channel created by another process can't be stopped from here: stopping
  needs the `resourceId` it was created with.
- The address must be HTTPS with a valid certificate.
- **A notification has no body.** Drive puts everything in `X-Goog-*` headers.
  `verify_webhook_notification(payload)` takes those headers as `payload` and compares
  `X-Goog-Channel-Id` and `X-Goog-Channel-Token` with the stored channel.
  `data_sources.web`'s router passes the request headers as the payload whenever a
  notification has no JSON body, so mounting the connector with `build_connectors_router`
  works as it does for SharePoint, and nothing extra needs wiring.
- The first notification after `create_webhook` carries `X-Goog-Resource-State: sync`. It
  announces the channel rather than a change, and is harmless to sync on.

The SharePoint doc's advice on overlapping runs (a poll and a webhook syncing at once)
applies here too.

## Permissions

`get_permissions(item)` maps Drive's permissions as follows:

- **Roles:** `owner` and `organizer` become `OWNER`; `fileOrganizer` and `writer` become
  `WRITER`; `commenter` becomes `COMMENTER`; `reader` becomes `READER`.
- **Principals:** `user`, `group`, `domain` and `anyone` map to the type of the same name.
  The principal is the email address, the domain, or `anyone`.

Permissions with any other role or principal type are left out.
