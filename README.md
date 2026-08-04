# Data Sources

> Unified, configurable connectors for enterprise and cloud data sources.

`data-sources` is an open-source Python framework that provides a unified interface for connecting to, authenticating with, and synchronizing content from external data sources.

Instead of implementing Google Drive, SharePoint, OneDrive, S3, or other providers separately, applications can rely on a single, consistent API and configuration model.

The goal of this project is to make data source integrations simple, reusable, and provider-agnostic.

---

# Scope

The project focuses on providing a common abstraction over multiple storage providers while hiding provider-specific implementation details.

Each connector exposes a consistent interface for:

- Authentication
- Connection validation
- File and folder enumeration
- Metadata retrieval
- Content download
- Incremental synchronization
- Change detection
- Permission retrieval
- Webhook support (where available)

The library is designed to be used by:

- AI & RAG platforms
- Enterprise search
- Knowledge management systems
- ETL pipelines
- Backup & synchronization tools
- Internal developer platforms
- Custom applications

---

# Design Principles

- Provider-agnostic API
- Configuration-driven
- Fully asynchronous
- Extensible plugin architecture
- Type-safe models
- Minimal external dependencies
- Production-ready authentication
- Enterprise-first

---

# Example

```python
from data_sources import create_connector

connector = create_connector(config)

await connector.validate()

async for item in connector.list():
    print(item.name)

content = await connector.download(item)
```

## HTTP API (optional)

Install the `api` extra to expose connectors over HTTP — item listing, metadata,
download, permissions, and webhook receivers — without writing any routes by hand:

```bash
pip install "axera-data-sources[api]"
```

```python
from fastapi import FastAPI
from data_sources.web import build_connectors_router

app = FastAPI()
app.include_router(build_connectors_router({"google_drive": connector}), prefix="/connectors")
```

Routes are generated from the `Connector` interface: `/webhooks` only appears when
`supports_webhooks` is set, and `/permissions` only when `supports_permissions` is set.

---

# Supported Data Sources

| Provider | Status |
|----------|:------:|
| Google Drive | ❌ Planned |
| Google Shared Drives | ❌ Planned |
| Microsoft OneDrive | ❌ Planned |
| SharePoint Online | ✅ Supported |
| Microsoft Teams Files | ❌ Planned |
| Dropbox | ❌ Planned |
| Box | ❌ Planned |
| Amazon S3 | ❌ Planned |
| Azure Blob Storage | ❌ Planned |
| Azure Data Lake Storage Gen2 | ❌ Planned |
| Google Cloud Storage | ❌ Planned |
| MinIO | ❌ Planned |
| SFTP | ❌ Planned |
| FTP / FTPS | ❌ Planned |
| SMB / CIFS | ❌ Planned |
| NFS | ❌ Planned |
| Local File System | ❌ Planned |
| WebDAV | ❌ Planned |
| Confluence | ❌ Planned |
| Notion | ❌ Planned |
| GitHub Repositories | ❌ Planned |
| GitLab Repositories | ❌ Planned |
| Bitbucket | ❌ Planned |
| Jira Attachments | ❌ Planned |

---

# Documentation

- [SharePoint connector](docs/sharepoint.md) — configuration, sync, item lookup, webhooks
- [Store driver](docs/store.md) — configuring SQLite/PostgreSQL, migrations, adding a driver

---

# Planned Features

- OAuth2 authentication manager
- Automatic token refresh
- Unified configuration schema
- Incremental synchronization
- Delta APIs
- File versioning
- Metadata extraction
- Permission mapping
- Watchers & webhooks
- Parallel downloads
- Retry policies
- Rate limiting
- Checkpoint management
- Pluggable authentication providers
- Pluggable storage providers

---

# Contributing

Contributions are welcome!

If you'd like to add support for a new provider, improve an existing connector, or suggest new features, feel free to open an Issue or submit a Pull Request.

---

# License

Apache License 2.0
