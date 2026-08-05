# Contributing

Thanks for considering a contribution to `data-sources`.

## Development setup

This project uses [Poetry](https://python-poetry.org/).

```bash
git clone https://github.com/AXERA-SYMLOGIC-SRL/data-sources.git
cd data-sources
poetry install
```

## Before opening a pull request

```bash
poetry run ruff check .
poetry run ruff format .
poetry run mypy
poetry run pytest
```

All four must pass. CI runs the same checks on every pull request.

## Adding a new connector

Connectors live under `src/data_sources/connectors/<provider>/` and implement the
`data_sources.core.Connector` base class. A connector must:

- Set a unique `provider` class attribute.
- Implement `connect`, `validate`, `list`, `get_metadata`, `download`, and `close`.
- Opt into `supports_sync`, `supports_permissions`, or `supports_webhooks` only if the
  corresponding methods are implemented.
- Register itself with `@register_connector("<provider>")`.
- Ship tests under `tests/` covering at least `validate`, `list`, and `download`.

## Logging

Every client (the class that actually talks to the provider — `SharepointClient`,
`SFTPClient`, etc.) gets its own child logger off `data_sources.core.logging.logger`,
named `<provider>.<module>`:

```python
from data_sources.core.logging import logger

class SFTPClient:
    def __init__(self, ...) -> None:
        self.logger = logger.getChild("sftp.client")
```

If the client exposes a `connect()` classmethod, it runs before an instance (and thus
`self.logger`) exists — module-level code needs its own reference to the same child
logger, which `__init__` then reuses instead of calling `getChild` again:

```python
_logger = logger.getChild("sftp.client")

class SFTPClient:
    def __init__(self, ...) -> None:
        self.logger = _logger

    @classmethod
    async def connect(cls, ...) -> SFTPClient:
        ...  # use _logger here
```

Log at the client level, not the connector level — the client is where the actual
network call happens. At minimum, log:

- **`info`** on a successful connect (`"Connected to <provider> as <who>"`) and on
  `close()` (`"<provider> connection closed"`).
- **`error`** on a connection or authentication failure, and on any other error the
  provider returns, before wrapping it and raising (e.g. `AuthenticationError`,
  `DataSourceError`). Include enough context (host, path, status code) to debug
  without needing to reproduce.
- **`warning`** for expected-but-noteworthy conditions the caller will likely handle,
  such as a `NotFoundError` or a rate-limit retry.

## Commit style

Keep commits focused and describe the *why*, not just the *what*. Reference the
provider or module affected when relevant (e.g. `sftp: handle expired host keys`).

## Reporting bugs and requesting features

Open a [GitHub Issue](https://github.com/AXERA-SYMLOGIC-SRL/data-sources/issues) with
enough detail to reproduce (provider, config shape, and stack trace where applicable).
