# The Store driver

Connectors that support sync, item lookup, or webhooks need somewhere to persist their own
state — a sync cursor, a synced-item index, a webhook subscription secret. `Store` is that
somewhere: a small abstraction over a SQLAlchemy async engine, one instance shared by every
connector in your process.

You don't need a `Store` at all for read-only use of a connector (`list`, `get_metadata`,
`download`) — only `sync`, `get_item`, and the `*_webhook` methods touch it.

## Configuring it

```python
from data_sources import StoreConfig

# Default: a local SQLite file, no config needed.
config = StoreConfig()

# Explicit SQLite path:
config = StoreConfig(driver="sqlite", url="sqlite+aiosqlite:///./my-app.db")

# PostgreSQL — requires the `postgresql` extra (`pip install "axera-data-sources[postgresql]"`)
# and an explicit `url`; there's no default to fall back to.
config = StoreConfig(
    driver="postgresql",
    url="postgresql+asyncpg://user:pass@host/db",
)
```

- **`driver`** — `"sqlite"` (default) or `"postgresql"`. Both are thin subclasses of
  `SQLAlchemyStore` that only differ in their default URL and which async driver they expect
  (`aiosqlite` / `asyncpg`).
- **`url`** — a SQLAlchemy async URL. Optional for SQLite (defaults to
  `sqlite+aiosqlite:///./data_sources.db` in the current working directory); required for
  PostgreSQL.
- **`options`** — passed straight through to SQLAlchemy's `create_async_engine` (pool size,
  `echo`, etc.), for whichever driver you're using.

## Using it

```python
from data_sources.store import init_store, init_connector

store = await init_store(config)  # connect() + migrate() to head, in one call
connector = await init_connector(connector_config, store)  # see below
...
await store.close()
```

`init_connector` does two things a bare `create_connector(config)` doesn't: it calls
`store.ensure_tables(connector.models)` so the tables that specific connector needs already
exist, then sets `connector.store = store`. Every store-backed connector method
(`_load_cursor`, `_save_item`, `create_webhook`, ...) assumes `self.store` is already set —
constructing a connector directly and skipping `init_connector` leaves those methods unusable.

One store instance can back several connectors — including different providers — at once;
each connector's models are namespaced by table name (and, within a table, usually a
provider-specific key) so they don't collide.

### `migrate()` vs `ensure_tables()`

These cover two different layers of schema and are easy to conflate:

- **`migrate()`** applies this library's own Alembic migrations (under
  `data_sources/store/migrations/`) up to head. It's for schema this library itself owns
  going forward — right now that history is empty (no core tables have shipped yet), but any
  connector added later that needs a *managed, versioned* migration path would go here.
- **`ensure_tables(models)`** creates a connector's declared tables directly from their
  ORM definitions if they don't already exist (`checkfirst=True`), with no migration history
  at all. This is what connectors currently use — e.g. `SharePointConnector.models` lists
  `SharePointSyncState`, `SharePointItemRecord`, `SharePointWebhookState`.

Both are idempotent and safe to call repeatedly; `init_store`/`init_connector` already call
them for you, so you generally won't call either directly.

### Sessions

```python
async with store.session() as session:
    ...
    await session.commit()
```

`session()` opens a plain SQLAlchemy `AsyncSession` (`expire_on_commit=False`). Connector
internals use this directly (see e.g. `SharePointConnector._load_cursor`); you'd only reach
for it yourself if you were writing a new connector's persistence methods.

## Adding a driver

A third backend (MySQL, a managed Postgres variant with different pooling defaults, ...) is a
subclass of `SQLAlchemyStore` registered under a driver name:

```python
from data_sources.store import register_store, SQLAlchemyStore

@register_store("mysql")
class MySQLStore(SQLAlchemyStore):
    driver = "mysql"
    # default_url: ClassVar[str | None] = None  # require an explicit url, like PostgreSQL
```

`StoreConfig(driver="mysql", ...)` then resolves to it via `create_store`/`init_store`. A
backend that isn't SQLAlchemy-based at all (e.g. a key-value store) would instead subclass
`Store` directly and implement `connect`, `migrate`, `ensure_tables`, `session`, and `close`
itself.
