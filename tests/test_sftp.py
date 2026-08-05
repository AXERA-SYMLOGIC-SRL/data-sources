from __future__ import annotations

import stat as stat_module
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import aioftp
import asyncssh
import pytest

from data_sources.config.schema import AuthConfig, ConnectorConfig, StoreConfig
from data_sources.connectors.sftp.connector import SFTPConnector
from data_sources.connectors.sftp.entries import RemoteClient, RemoteEntry
from data_sources.connectors.sftp.ftp_client import FTPClient
from data_sources.connectors.sftp.models import SFTPItemRecord, SFTPSyncState
from data_sources.connectors.sftp.sftp_client import SFTPClient
from data_sources.core.exceptions import (
    AuthenticationError,
    ConfigurationError,
    NotFoundError,
)
from data_sources.core.exceptions import (
    ConnectionError as DataSourceConnectionError,
)
from data_sources.core.models import ChangeType, Item, ItemType, SyncCursor
from data_sources.store import init_connector, init_store

SFTP_URL = "sftp://sftp.example.com/data"
FTP_URL = "ftp://ftp.example.com/data"


def _entry(
    path: str,
    *,
    is_dir: bool = False,
    size: int | None = 123,
    mtime: datetime | None = datetime(2026, 1, 1, tzinfo=UTC),
    permissions: int = 0o100644,
) -> RemoteEntry:
    return RemoteEntry(
        path=path,
        name=path.rsplit("/", 1)[-1],
        is_dir=is_dir,
        size=None if is_dir else size,
        mtime=mtime,
        permissions=(stat_module.S_IFDIR | 0o755) if is_dir else permissions,
    )


class FakeRemoteClient:
    """Duck-types `RemoteClient`, so connector tests don't need a real server on
    either side of the sftp/ftp split — the connector doesn't care which protocol
    it's talking to once `connect()` has produced a client."""

    def __init__(self) -> None:
        self.closed = False
        self.downloaded: list[str] = []
        self._tree: dict[str, list[RemoteEntry]] = {}
        self._walked: list[RemoteEntry] = []

    def set_children(self, path: str, entries: list[RemoteEntry]) -> None:
        self._tree[path] = entries

    def set_walk(self, entries: list[RemoteEntry]) -> None:
        self._walked = entries

    async def stat(self, path: str) -> RemoteEntry:
        return _entry(path)

    async def listdir(self, path: str) -> AsyncIterator[RemoteEntry]:
        for entry in self._tree.get(path, []):
            yield entry

    async def walk(self, path: str) -> AsyncIterator[RemoteEntry]:
        for entry in self._walked:
            yield entry

    async def download(self, path: str) -> AsyncIterator[bytes]:
        self.downloaded.append(path)
        for chunk in (b"chunk-1", b"chunk-2"):
            yield chunk

    async def close(self) -> None:
        self.closed = True


def make_connector(url: str = SFTP_URL, **options: Any) -> tuple[SFTPConnector, FakeRemoteClient]:
    config = ConnectorConfig(provider="sftp", options={"url": url, **options})
    connector = SFTPConnector(config)
    fake_client = FakeRemoteClient()
    connector._client = cast(RemoteClient, fake_client)
    return connector, fake_client


class TestConnectorConstruction:
    def test_requires_url_option(self) -> None:
        with pytest.raises(ConfigurationError):
            SFTPConnector(ConnectorConfig(provider="sftp"))

    def test_rejects_unsupported_scheme(self) -> None:
        with pytest.raises(ConfigurationError):
            SFTPConnector(
                ConnectorConfig(provider="sftp", options={"url": "https://example.com/data"})
            )

    def test_requires_host(self) -> None:
        with pytest.raises(ConfigurationError):
            SFTPConnector(ConnectorConfig(provider="sftp", options={"url": "sftp:///data"}))

    def test_defaults_port_per_scheme(self) -> None:
        sftp_connector = SFTPConnector(ConnectorConfig(provider="sftp", options={"url": SFTP_URL}))
        ftp_connector = SFTPConnector(ConnectorConfig(provider="sftp", options={"url": FTP_URL}))

        assert sftp_connector._port == 22
        assert ftp_connector._port == 21

    def test_explicit_port_overrides_default(self) -> None:
        connector = SFTPConnector(
            ConnectorConfig(provider="sftp", options={"url": "sftp://host:2222/data"})
        )

        assert connector._port == 2222

    @pytest.mark.asyncio
    async def test_connect_requires_username(self) -> None:
        config = ConnectorConfig(
            provider="sftp",
            options={"url": SFTP_URL},
            auth=AuthConfig(type="password", credentials={"password": "secret"}),
        )
        connector = SFTPConnector(config)

        with pytest.raises(ConfigurationError):
            await connector.connect()

    @pytest.mark.asyncio
    async def test_connect_requires_password_or_private_key_for_sftp(self) -> None:
        config = ConnectorConfig(
            provider="sftp",
            options={"url": SFTP_URL},
            auth=AuthConfig(type="password", credentials={"username": "alice"}),
        )
        connector = SFTPConnector(config)

        with pytest.raises(ConfigurationError):
            await connector.connect()

    def test_client_property_requires_connect(self) -> None:
        connector = SFTPConnector(ConnectorConfig(provider="sftp", options={"url": SFTP_URL}))

        with pytest.raises(DataSourceConnectionError):
            _ = connector.client


class TestSchemeDispatch:
    @pytest.mark.asyncio
    async def test_sftp_scheme_uses_sftp_client(self) -> None:
        config = ConnectorConfig(
            provider="sftp",
            options={"url": SFTP_URL},
            auth=AuthConfig(type="password", credentials={"username": "alice", "password": "x"}),
        )
        connector = SFTPConnector(config)
        fake_client = FakeRemoteClient()

        with patch.object(
            SFTPClient, "connect", AsyncMock(return_value=fake_client)
        ) as sftp_connect:
            await connector.connect()

        sftp_connect.assert_awaited_once()
        assert sftp_connect.await_args is not None
        assert sftp_connect.await_args.args[0] == "sftp.example.com"
        assert sftp_connect.await_args.args[1] == 22
        assert connector._client is fake_client

    @pytest.mark.asyncio
    async def test_ftp_scheme_uses_ftp_client(self) -> None:
        config = ConnectorConfig(
            provider="sftp",
            options={"url": FTP_URL},
            auth=AuthConfig(type="password", credentials={"username": "alice"}),
        )
        connector = SFTPConnector(config)
        fake_client = FakeRemoteClient()

        with patch.object(FTPClient, "connect", AsyncMock(return_value=fake_client)) as ftp_connect:
            await connector.connect()

        ftp_connect.assert_awaited_once()
        assert ftp_connect.await_args is not None
        assert ftp_connect.await_args.args[0] == "ftp.example.com"
        assert ftp_connect.await_args.args[1] == 21
        assert connector._client is fake_client


class TestList:
    @pytest.mark.asyncio
    async def test_recursive_yields_only_files_from_walk(self) -> None:
        connector, fake_client = make_connector()
        fake_client.set_walk(
            [_entry("/data/a.txt"), _entry("/data/sub/b.pdf")],
        )

        items = [item async for item in connector.list(recursive=True)]

        assert [item.id for item in items] == ["/data/a.txt", "/data/sub/b.pdf"]
        assert all(item.type == ItemType.FILE for item in items)

    @pytest.mark.asyncio
    async def test_recursive_skips_excluded_paths(self) -> None:
        connector, fake_client = make_connector(excluded_paths=["Archive"])
        fake_client.set_walk(
            [_entry("/data/keep.txt"), _entry("/data/Archive/old.txt")],
        )

        items = [item async for item in connector.list(recursive=True)]

        assert [item.id for item in items] == ["/data/keep.txt"]

    @pytest.mark.asyncio
    async def test_non_recursive_includes_subfolders(self) -> None:
        connector, fake_client = make_connector()
        fake_client.set_children("/data", [_entry("/data/a.txt"), _entry("/data/sub", is_dir=True)])

        items = [item async for item in connector.list(recursive=False)]

        assert [item.id for item in items] == ["/data/a.txt", "/data/sub"]
        assert items[0].type == ItemType.FILE
        assert items[1].type == ItemType.FOLDER

    @pytest.mark.asyncio
    async def test_non_recursive_uses_given_path(self) -> None:
        connector, fake_client = make_connector()
        fake_client.set_children("/data/sub", [_entry("/data/sub/c.txt")])

        items = [item async for item in connector.list("/data/sub")]

        assert [item.id for item in items] == ["/data/sub/c.txt"]


class TestSync:
    @pytest.mark.asyncio
    async def test_classifies_created_updated_deleted_and_carries_cursor_once(self) -> None:
        connector, fake_client = make_connector()
        previous = SyncCursor(
            token='{"/data/changed.txt": "2026-01-01T00:00:00+00:00", '
            '"/data/gone.txt": "2026-01-01T00:00:00+00:00"}'
        )
        fake_client.set_walk(
            [
                _entry("/data/new.txt"),
                _entry("/data/changed.txt", mtime=datetime(2026, 1, 2, tzinfo=UTC)),
            ]
        )

        changes = [change async for change in connector.sync(previous)]

        assert [(c.item_id, c.type) for c in changes] == [
            ("/data/new.txt", ChangeType.CREATED),
            ("/data/changed.txt", ChangeType.UPDATED),
            ("/data/gone.txt", ChangeType.DELETED),
        ]
        assert changes[0].cursor is None
        assert changes[1].cursor is None
        assert changes[2].cursor is not None
        assert changes[2].item is None

    @pytest.mark.asyncio
    async def test_first_sync_treats_everything_as_created(self) -> None:
        connector, fake_client = make_connector()
        fake_client.set_walk([_entry("/data/a.txt")])

        changes = [change async for change in connector.sync()]

        assert [c.type for c in changes] == [ChangeType.CREATED]

    @pytest.mark.asyncio
    async def test_unchanged_tree_yields_no_changes(self) -> None:
        connector, fake_client = make_connector()
        previous = SyncCursor(token='{"/data/a.txt": "2026-01-01T00:00:00+00:00"}')
        fake_client.set_walk([_entry("/data/a.txt")])

        changes = [change async for change in connector.sync(previous)]

        assert changes == []


@pytest.mark.asyncio
async def test_download_delegates_to_client() -> None:
    connector, fake_client = make_connector()
    item = await connector.get_metadata("/data/a.txt")

    chunks = [chunk async for chunk in connector.download(item)]

    assert chunks == [b"chunk-1", b"chunk-2"]
    assert fake_client.downloaded == ["/data/a.txt"]


async def _make_store_connector(tmp_path: Path) -> tuple[SFTPConnector, FakeRemoteClient, Any]:
    store = await init_store(StoreConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'sftp.db'}"))
    connector = await init_connector(
        ConnectorConfig(provider="sftp", name="prod-drop", options={"url": SFTP_URL}),
        store,
    )
    assert isinstance(connector, SFTPConnector)
    fake_client = FakeRemoteClient()
    connector._client = cast(RemoteClient, fake_client)
    return connector, fake_client, store


@pytest.mark.asyncio
async def test_sync_cursor_persists_across_load_and_commit(tmp_path: Path) -> None:
    connector, _fake_client, store = await _make_store_connector(tmp_path)
    try:
        assert await connector._load_cursor() is None

        await connector._commit_cursor(SyncCursor(token='{"/data/a.txt": "x"}'))
        assert await connector._load_cursor() == SyncCursor(token='{"/data/a.txt": "x"}')

        await connector._commit_cursor(SyncCursor(token='{"/data/a.txt": "y"}'))
        assert await connector._load_cursor() == SyncCursor(token='{"/data/a.txt": "y"}')

        async with store.session() as session:
            rows = (await session.execute(SFTPSyncState.__table__.select())).all()
            assert len(rows) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_get_item_resolves_a_saved_item_without_a_live_call(tmp_path: Path) -> None:
    connector, _fake_client, store = await _make_store_connector(tmp_path)
    try:
        assert await connector.get_item("/data/a.txt") is None

        item = Item(id="/data/a.txt", name="a.txt", type=ItemType.FILE)
        await connector._save_item(item)
        assert await connector.get_item("/data/a.txt") == item

        updated = Item(id="/data/a.txt", name="a.txt", type=ItemType.FILE, size=456)
        await connector._save_item(updated)
        assert await connector.get_item("/data/a.txt") == updated

        await connector._delete_item("/data/a.txt")
        assert await connector.get_item("/data/a.txt") is None

        async with store.session() as session:
            rows = (await session.execute(SFTPItemRecord.__table__.select())).all()
            assert rows == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sync_in_background_indexes_items_by_path(tmp_path: Path) -> None:
    connector, fake_client, store = await _make_store_connector(tmp_path)
    try:
        fake_client.set_walk([_entry("/data/keep.txt")])

        async def on_change(change: Any) -> None:
            pass

        await connector.sync_in_background(on_change)

        kept = await connector.get_item("/data/keep.txt")
        assert kept is not None
        assert kept.name == "keep.txt"
    finally:
        await store.close()


class TestSFTPClientEntryMapping:
    def test_to_entry_reports_directory_from_permissions(self) -> None:
        from data_sources.connectors.sftp.sftp_client import _to_entry

        attrs = asyncssh.SFTPAttrs()
        attrs.permissions = stat_module.S_IFDIR | 0o755
        attrs.mtime = 1735689600  # 2025-01-01T00:00:00Z

        entry = _to_entry("/data/sub", attrs)

        assert entry.is_dir is True
        assert entry.name == "sub"
        assert entry.mtime == datetime(2025, 1, 1, tzinfo=UTC)

    def test_to_entry_reports_file_from_permissions(self) -> None:
        from data_sources.connectors.sftp.sftp_client import _to_entry

        attrs = asyncssh.SFTPAttrs()
        attrs.permissions = stat_module.S_IFREG | 0o644
        attrs.size = 42

        entry = _to_entry("/data/a.txt", attrs)

        assert entry.is_dir is False
        assert entry.size == 42


class TestSFTPClientAgainstAsyncssh:
    @pytest.mark.asyncio
    async def test_connect_wraps_permission_denied_as_authentication_error(self) -> None:
        with (
            patch(
                "data_sources.connectors.sftp.sftp_client.asyncssh.connect",
                AsyncMock(side_effect=asyncssh.PermissionDenied("denied")),
            ),
            pytest.raises(AuthenticationError),
        ):
            await SFTPClient.connect("sftp.example.com", 22, "alice", password="wrong")

    @pytest.mark.asyncio
    async def test_stat_raises_not_found_for_missing_path(self) -> None:
        mock_sftp = AsyncMock()
        mock_sftp.stat = AsyncMock(side_effect=asyncssh.SFTPNoSuchFile("no such file"))
        client = SFTPClient(AsyncMock(), mock_sftp)

        with pytest.raises(NotFoundError):
            await client.stat("/missing")

    @pytest.mark.asyncio
    async def test_listdir_skips_dot_entries_and_builds_full_paths(self) -> None:
        mock_sftp = AsyncMock()
        entries = []
        for name in (".", "..", "a.txt"):
            n = asyncssh.SFTPName(name)
            n.attrs = asyncssh.SFTPAttrs()
            n.attrs.permissions = stat_module.S_IFREG | 0o644
            entries.append(n)
        mock_sftp.readdir = AsyncMock(return_value=entries)
        client = SFTPClient(AsyncMock(), mock_sftp)

        results = [entry async for entry in client.listdir("/data")]

        assert [entry.path for entry in results] == ["/data/a.txt"]

    @pytest.mark.asyncio
    async def test_walk_recurses_into_directories_and_skips_symlinks(self) -> None:
        client = SFTPClient(AsyncMock(), AsyncMock())

        async def fake_listdir(path: str) -> AsyncIterator[RemoteEntry]:
            if path == "/data":
                for e in (
                    _entry("/data/sub", is_dir=True),
                    _entry("/data/a.txt"),
                    RemoteEntry(
                        path="/data/link",
                        name="link",
                        is_dir=False,
                        size=None,
                        mtime=None,
                        permissions=stat_module.S_IFLNK | 0o777,
                    ),
                ):
                    yield e
            elif path == "/data/sub":
                yield _entry("/data/sub/b.txt")

        client.listdir = fake_listdir  # type: ignore[method-assign]

        results = [entry async for entry in client.walk("/data")]

        assert sorted(entry.path for entry in results) == ["/data/a.txt", "/data/sub/b.txt"]


async def _start_ftp_server() -> tuple[aioftp.Server, int]:
    users = [aioftp.User(permissions=[aioftp.Permission("/", readable=True, writable=True)])]
    # aioftp types `MemoryPathIO` as `AbstractPathIO[PurePosixPath]` but `Server`
    # wants `AbstractPathIO[Path]` — a mismatch in aioftp's own stubs, not ours.
    server = aioftp.Server(users, path_io_factory=aioftp.MemoryPathIO)  # type: ignore[arg-type]
    await server.start("127.0.0.1", 0)
    port = server.server.sockets[0].getsockname()[1]
    return server, port


class TestFTPClientAgainstAioftp:
    @pytest.mark.asyncio
    async def test_stat_list_walk_and_download_roundtrip(self) -> None:
        server, port = await _start_ftp_server()
        try:
            async with aioftp.Client.context("127.0.0.1", port, "anonymous", "anon@") as setup:
                await setup.make_directory("sub")
                async with setup.upload_stream("a.txt") as stream:
                    await stream.write(b"hello world")
                async with setup.upload_stream("sub/b.txt") as stream:
                    await stream.write(b"nested")

            client = await FTPClient.connect("127.0.0.1", port, "anonymous", password="anon@")
            try:
                stat_entry = await client.stat("/a.txt")
                assert stat_entry.is_dir is False
                assert stat_entry.size == 11

                listed = sorted([entry.path async for entry in client.listdir("/")])
                assert listed == ["/a.txt", "/sub"]

                walked = sorted([entry.path async for entry in client.walk("/")])
                assert walked == ["/a.txt", "/sub/b.txt"]

                chunks = [chunk async for chunk in client.download("/a.txt")]
                assert b"".join(chunks) == b"hello world"
            finally:
                await client.close()
        finally:
            await server.close()

    @pytest.mark.asyncio
    async def test_stat_raises_not_found_for_missing_path(self) -> None:
        server, port = await _start_ftp_server()
        try:
            client = await FTPClient.connect("127.0.0.1", port, "anonymous", password="anon@")
            try:
                with pytest.raises(NotFoundError):
                    await client.stat("/missing.txt")
            finally:
                await client.close()
        finally:
            await server.close()

    @pytest.mark.asyncio
    async def test_connect_wraps_bad_login_as_authentication_error(self) -> None:
        users = [
            aioftp.User(
                "validuser",
                "validpass",
                permissions=[aioftp.Permission("/", readable=True, writable=True)],
            )
        ]
        # aioftp types `MemoryPathIO` as `AbstractPathIO[PurePosixPath]` but `Server`
        # wants `AbstractPathIO[Path]` — a mismatch in aioftp's own stubs, not ours.
        server = aioftp.Server(users, path_io_factory=aioftp.MemoryPathIO)  # type: ignore[arg-type]
        await server.start("127.0.0.1", 0)
        port = server.server.sockets[0].getsockname()[1]
        try:
            with pytest.raises(AuthenticationError):
                await FTPClient.connect("127.0.0.1", port, "validuser", password="wrong-password")
        finally:
            await server.close()
