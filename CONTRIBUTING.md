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

## Commit style

Keep commits focused and describe the *why*, not just the *what*. Reference the
provider or module affected when relevant (e.g. `sftp: handle expired host keys`).

## Reporting bugs and requesting features

Open a [GitHub Issue](https://github.com/AXERA-SYMLOGIC-SRL/data-sources/issues) with
enough detail to reproduce (provider, config shape, and stack trace where applicable).
