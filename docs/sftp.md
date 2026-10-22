# SFTP connector

`SFTPConnector` (`provider = "sftp"`) connects to a single SFTP **or FTP** server,
addressed via `config.options["url"]` and rooted at that URL's path. To cover a second
server (or a second root on the same server), configure a second connector instance.

| Capability | Supported |
|---|:---:|
| `supports_sync` (incremental, via full-tree diff) | ✅ |
| `supports_item_lookup` (`get_item` without a live call) | ✅ |
| `supports_webhooks` | ❌ |
| `supports_permissions` | ❌ |

Neither protocol exposes a delta/change feed or a webhook mechanism, unlike Graph-backed
connectors — see [Incremental sync](#incremental-sync) below for what `sync()` does instead.

## Configuring it

```python
from data_sources import ConnectorConfig
from data_sources.config import AuthConfig

config = ConnectorConfig(
    provider="sftp",
    name="reports-drop",  # see "Why set `name`" below
    auth=AuthConfig(
        type="password",  # or "private_key" for sftp://
        credentials={
            "username": "...",
            "password": "...",  # or "private_key" (+ optional "passphrase") for sftp://
            # "known_hosts": "...",  # sftp:// only; omitting it disables host key checking
        },
    ),
    options={
        "url": "sftp://sftp.example.com/reports",  # or "ftp://ftp.example.com/reports"
        "excluded_paths": ["archive"],  # optional, relative to the url's path
    },
)
```

- **`options.url`** — its scheme picks the wire protocol: `sftp://` uses `SFTPClient`
  (asyncssh), `ftp://` uses `FTPClient` (aioftp, **plain FTP only** — FTPS isn't
  implemented). The port defaults per scheme (22 for `sftp://`, 21 for `ftp://`) but can
  be given explicitly (`sftp://host:2222/reports`); the URL's path becomes `root_path` —
  everything under it is in scope for `list()` and `sync()`. The connector raises
  `ConfigurationError` at construction time for any other scheme, a missing host, or a
  missing `url` altogether.
- **`auth.credentials.username`** — always required, for both schemes.
- **`auth.credentials.password`** / **`private_key`** — for `sftp://`, one of the two is
  required (`private_key` is a PEM-encoded string, optionally with `passphrase`); the
  connector raises `ConfigurationError` at `connect()` time if neither is given. For
  `ftp://`, `password` is optional (omit it, or use a blank one, for anonymous FTP).
- **`auth.credentials.known_hosts`** — `sftp://` only; passed straight through to
  asyncssh's host key verification. Leaving it unset disables host key checking
  entirely, which is convenient for a first connection but means a
  machine-in-the-middle isn't detected — set it once you know the server's host key.
- **`options.excluded_paths`**: folders, relative to the url's path, to skip in `list()`
  and `sync()`, matched by whole path segment. `["archive"]` skips `reports/archive/...`
  but not `reports/archive2025/...`.

FTP entries never carry a `permissions` value in `Item.metadata` (unlike SFTP, whose
POSIX mode bits are always available) — FTP's MLSD/LIST facts don't reliably expose them
across servers.

### Why set `name`

The connector persists its sync snapshot, item index, and store rows keyed by
`config.name` if set, falling back to the full `options.url` otherwise (see
`_sync_key`). Set `name` explicitly and keep it stable — if the URL ever changes while
`name` doesn't, the connector keeps resuming from the same state instead of silently
starting a fresh sync under a new key.

## Using it

An `SFTPConnector` needs a `Store` to back `sync` and item lookup — those methods
read/write `self.store`, set by `init_connector`, not by the constructor. Read-only use
(`list`, `get_metadata`, `download`) works without one.

```python
from data_sources.store import init_store, init_connector

store = await init_store()  # see store.md — defaults to a local SQLite file
connector = await init_connector(config, store)  # creates its tables, sets connector.store

await connector.connect()  # opens the SSH/SFTP or FTP session, per the url's scheme
await connector.validate()  # True if root_path is reachable

async for item in connector.list(recursive=True):
    print(item.name)

await connector.close()
```

Only regular files are surfaced by `list(recursive=True)` and `sync()` — directories are
skipped while walking the tree (for SFTP, so are symlinks and other special files).
`list(recursive=False)` browses a single directory level and includes subfolders
(`ItemType.FOLDER`), useful for building a picker UI. Pass `path` to browse anywhere
under `root_path`, either relative to it (`"invoices/2026"`) or absolute and inside it
(`"/reports/invoices/2026"`).

### `root_path` is a boundary

`list(path)`, `get_metadata(item_id)` and `download(item)` refuse (with `NotFoundError`)
any path that leads outside `root_path`. That covers `..` segments, an absolute path
elsewhere, and a path in an excluded folder. Symlinks are covered too:
- `get_metadata` refuses a path that is itself a symlink;
- every path is resolved on the server (`realpath`) and must still lie under the
  resolved `root_path`, so a linked folder can't lead out.

`download` checks `item.id` again rather than trusting the `Item` it's given. FTP exposes
no symlinks, so there the check is on the normalized path alone.

### Paged listing

`list_page(path, recursive=..., page_size=..., page_token=..., search=...)` returns one
`ItemPage` at a time. Neither protocol pages natively, so this is the library's default:
each call walks `list()` from the start and skips what earlier pages returned. It's fine
for browsing a folder, but slow for very large trees.

### Writing

```python
from data_sources.core import ConflictBehavior

item = await connector.upload("out/2026", "report.pdf", pdf_bytes)  # folders made as needed
item = await connector.upload("out/2026", "report.pdf", newer, on_conflict=ConflictBehavior.REPLACE)
await connector.create_folder("out/2027")
await connector.delete(item.id)  # permanent: neither protocol has a bin
```

- **Safe writes.** Content is written to a hidden temporary name (`.report.pdf.<id>.part`)
  beside the target, then renamed onto it, so a reader never sees a half-written file.
  `REPLACE` uses OpenSSH's atomic `posix-rename` where the server offers it. Otherwise,
  and always on FTP, the old file is removed first, which is not atomic.
- **Folders.** Missing folders are made one level at a time, and each existing level is
  checked first. A symlink in the path is refused, so nothing is created through a link
  to outside `root_path`, and a file in the way raises `ConflictError`.
- **A taken name:**
  - `FAIL` raises `ConflictError`;
  - `REPLACE` writes over a file, but never over a folder or a symlink;
  - `RENAME` writes beside it as `report (1).pdf`.
- **Not one step.** Checking the name and writing are separate, so a file that appears
  in between may be written over (SFTP) or refused by the server.
- **Excluded folders.** Writes may go into an excluded folder, never outside `root_path`.
- **Deleting.** `delete` removes a file or an *empty* folder, and refuses symlinks and
  `root_path` itself.

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

Neither SFTP nor FTP expose a delta API, so `sync()` recursively walks `root_path` on
every call and diffs the resulting `path -> mtime` snapshot against the previous one
(persisted as `SFTPSyncState.snapshot`, JSON-encoded): a path missing a prior entry is
`CREATED`, a changed `mtime` is `UPDATED`, and a previously-seen path missing from the
new walk is `DELETED`. This means each `sync()` call costs a full tree listing — there's
no cheaper incremental primitive to fall back to over either protocol.

`sync_in_background` loads the connector's own persisted snapshot, walks the tree via
`sync()`, calls `on_change` per change, then commits that change's cursor — so a crash
between the two redelivers that one change next run; `on_change` must tolerate that. It
also updates the connector's own item index as it goes, so a later call elsewhere with
just a path — `await connector.get_item(item_id)` — can resolve the `Item` without
re-walking the tree.

`sync_in_background` runs until the walk is exhausted, once. Call it again — on a timer —
to pick up new changes; it does not loop or poll on its own, and there's no webhook to
trigger it in reaction to a change.
