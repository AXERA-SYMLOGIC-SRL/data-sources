from __future__ import annotations

import copy
import json
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import jwt

from data_sources.config.schema import AuthConfig, ConnectorConfig, StoreConfig
from data_sources.connectors.google_drive import client as drive_client
from data_sources.connectors.google_drive.auth import (
    TOKEN_URL,
    RefreshTokenCredential,
    ServiceAccountCredential,
    credential_from_config,
)
from data_sources.connectors.google_drive.client import (
    FOLDER_MIME,
    SHORTCUT_MIME,
    GoogleDriveClient,
)
from data_sources.connectors.google_drive.connector import GoogleDriveConnector
from data_sources.connectors.google_drive.scope import parse_folder
from data_sources.core.exceptions import (
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    DataSourceError,
    InvalidPageTokenError,
    NotFoundError,
    RateLimitError,
    UnsupportedOperationError,
)
from data_sources.core.models import (
    Change,
    ChangeType,
    ConflictBehavior,
    Item,
    ItemType,
    PermissionPrincipalType,
    PermissionRole,
    SyncCursor,
)
from data_sources.core.paging import encode_token
from data_sources.store import init_connector, init_store
from data_sources.store.base import Store

PDF = "application/pdf"
DOC = "application/vnd.google-apps.document"
FORM = "application/vnd.google-apps.form"
ROOT = "folderroot"


class FakeCredential:
    def __init__(self) -> None:
        self.closed = False

    async def get_token(self) -> str:
        return "fake-token"

    async def close(self) -> None:
        self.closed = True


class FakeDrive:
    """An in-memory Drive behind `httpx.MockTransport` — files, their parents, a change log
    (a page token is an index into it), channels and permissions."""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        self.content: dict[str, bytes] = {}
        self.log: list[dict[str, Any]] = []
        self.page_size = 50
        self.watched: list[dict[str, Any]] = []
        self.stopped: list[dict[str, Any]] = []
        self.permissions: dict[str, list[dict[str, Any]]] = {}
        self.requests: list[httpx.Request] = []
        self.created = 0
        self.uploads: list[str] = []
        self.sessions: dict[str, dict[str, Any]] = {}
        self.add("myroot", "My Drive", None, folder=True)
        self.add(ROOT, "Reports", "myroot", folder=True)

    def add(
        self,
        file_id: str,
        name: str,
        parent: str | None,
        *,
        folder: bool = False,
        mime: str = PDF,
        content: bytes = b"",
        **extra: Any,
    ) -> dict[str, Any]:
        raw: dict[str, Any] = {
            "id": file_id,
            "name": name,
            "mimeType": FOLDER_MIME if folder else mime,
            "parents": [parent] if parent else [],
            "trashed": False,
            "createdTime": "2026-01-01T00:00:00Z",
            "modifiedTime": "2026-01-01T00:00:00Z",
            "webViewLink": f"https://drive.google.com/{file_id}",
            **extra,
        }
        if not folder and not mime.startswith("application/vnd.google-apps."):
            raw["size"] = str(len(content))
            raw["md5Checksum"] = f"md5-{file_id}"
        self.files[file_id] = raw
        self.content[file_id] = content
        return raw

    def record(self, file_id: str, *, removed: bool = False) -> None:
        entry: dict[str, Any] = {
            "changeType": "file",
            "fileId": file_id,
            "time": "2026-02-01T00:00:00Z",
            "removed": removed,
        }
        if not removed:
            entry["file"] = copy.deepcopy(self.files[file_id])
        self.log.append(entry)

    def create(self, file_id: str, name: str, parent: str, **kwargs: Any) -> None:
        self.add(file_id, name, parent, **kwargs)
        self.record(file_id)

    def edit(self, file_id: str) -> None:
        self.files[file_id]["modifiedTime"] = "2026-02-01T00:00:00Z"
        self.record(file_id)

    def move(self, file_id: str, parent: str) -> None:
        self.files[file_id]["parents"] = [parent]
        self.record(file_id)

    def rename(self, file_id: str, name: str) -> None:
        self.files[file_id]["name"] = name
        self.record(file_id)

    def trash(self, file_id: str) -> None:
        self.files[file_id]["trashed"] = True
        self.record(file_id)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.startswith("/upload/drive/v3/"):
            return self._upload(request)
        path = request.url.path.removeprefix("/drive/v3/")
        params = request.url.params

        if request.method != "GET" and path.startswith("files"):
            return self._write(request, path)

        if path == "changes/startPageToken":
            return httpx.Response(200, json={"startPageToken": str(len(self.log))})
        if path == "changes":
            start = int(params["pageToken"])
            end = start + self.page_size
            body: dict[str, Any] = {"changes": self.log[start:end]}
            if end < len(self.log):
                body["nextPageToken"] = str(end)
            else:
                body["newStartPageToken"] = str(len(self.log))
            return httpx.Response(200, json=body)
        if path == "changes/watch":
            body = json.loads(request.content)
            self.watched.append({**body, "params": dict(params)})
            return httpx.Response(
                200,
                json={
                    "id": body["id"],
                    "resourceId": f"resource-{len(self.watched)}",
                    "expiration": str(body["expiration"]),
                },
            )
        if path == "channels/stop":
            self.stopped.append(json.loads(request.content))
            return httpx.Response(204)
        if path == "files":
            matched = self._query(params["q"])
            if params.get("orderBy") == "folder,name":
                matched.sort(key=lambda raw: (raw["mimeType"] != FOLDER_MIME, raw["name"]))
            start = int(params.get("pageToken", 0))
            end = start + int(params.get("pageSize", 1000))
            listed: dict[str, Any] = {"files": matched[start:end]}
            if end < len(matched):
                listed["nextPageToken"] = str(end)
            return httpx.Response(200, json=listed)

        match = re.fullmatch(r"files/([^/]+)(/export|/permissions)?", path)
        assert match, f"unexpected request {request.url}"
        file_id = "myroot" if match.group(1) == "root" else match.group(1)
        if file_id not in self.files:
            return httpx.Response(404, json={"error": {"message": "File not found"}})
        if match.group(2) == "/permissions":
            return httpx.Response(200, json={"permissions": self.permissions.get(file_id, [])})
        if match.group(2) == "/export":
            return httpx.Response(200, content=f"exported:{params['mimeType']}".encode())
        if params.get("alt") == "media":
            return httpx.Response(200, content=self.content[file_id])
        return httpx.Response(200, json=self.files[file_id])

    def _query(self, query: str) -> list[dict[str, Any]]:
        parent = re.search(r"'([^']+)' in parents", query)
        name = re.search(r"name = '([^']+)'", query)
        return [
            raw
            for raw in self.files.values()
            if not raw["trashed"]
            and (parent is None or parent.group(1) in raw["parents"])
            and (name is None or raw["name"] == name.group(1))
            and (f"mimeType = '{FOLDER_MIME}'" not in query or raw["mimeType"] == FOLDER_MIME)
            and (f"mimeType != '{FOLDER_MIME}'" not in query or raw["mimeType"] != FOLDER_MIME)
        ]

    def _new_id(self) -> str:
        self.created += 1
        return f"new{self.created}"

    def _write(self, request: httpx.Request, path: str) -> httpx.Response:
        """Metadata writes: create a folder, trash a file, delete one."""
        if request.method == "POST" and path == "files":
            body = json.loads(request.content)
            raw = self.add(self._new_id(), body["name"], body["parents"][0], folder=True)
            return httpx.Response(200, json=raw)
        file_id = path.removeprefix("files/")
        if file_id not in self.files:
            return httpx.Response(404, json={"error": {"message": "File not found"}})
        if request.method == "PATCH":
            self.files[file_id].update(json.loads(request.content))
            return httpx.Response(200, json=self.files[file_id])
        if request.method == "DELETE":
            del self.files[file_id]
            return httpx.Response(204)
        raise AssertionError(f"unexpected write {request.method} {request.url}")

    def _upload(self, request: httpx.Request) -> httpx.Response:
        """The upload endpoint: multipart creates, media updates, resumable sessions."""
        path = request.url.path.removeprefix("/upload/drive/v3/")
        kind = request.url.params.get("uploadType")
        self.uploads.append(kind or "chunk")
        if path.startswith("session/"):
            return self._chunk(request, path.removeprefix("session/"))
        if kind == "multipart":
            boundary = request.headers["Content-Type"].split("boundary=")[1].encode()
            parts = request.content.split(b"--" + boundary)
            metadata = json.loads(parts[1].split(b"\r\n\r\n", 1)[1].removesuffix(b"\r\n"))
            head, content = parts[2].split(b"\r\n\r\n", 1)
            mime = head.decode().split("Content-Type: ")[1].strip()
            return httpx.Response(200, json=self._finish(None, metadata, mime, content[:-2]))
        if kind == "media":
            file_id = path.removeprefix("files/")
            mime = request.headers["Content-Type"]
            return httpx.Response(200, json=self._finish(file_id, None, mime, request.content))
        if kind == "resumable":
            session = str(len(self.sessions))
            updated = path.removeprefix("files/") if path != "files" else None
            self.sessions[session] = {
                "file_id": updated,
                "metadata": json.loads(request.content) if updated is None else None,
                "mime": request.headers["X-Upload-Content-Type"],
                "content": b"",
            }
            location = f"https://www.googleapis.com/upload/drive/v3/session/{session}"
            return httpx.Response(200, headers={"Location": location})
        raise AssertionError(f"unexpected upload {request.url}")

    def _chunk(self, request: httpx.Request, session_id: str) -> httpx.Response:
        session = self.sessions[session_id]
        session["content"] += request.content
        span, total = request.headers["Content-Range"].removeprefix("bytes ").split("/")
        if int(span.split("-")[1]) + 1 < int(total):
            return httpx.Response(308)
        raw = self._finish(
            session["file_id"], session["metadata"], session["mime"], session["content"]
        )
        return httpx.Response(200, json=raw)

    def _finish(
        self, file_id: str | None, metadata: dict[str, Any] | None, mime: str, content: bytes
    ) -> dict[str, Any]:
        if file_id is None:
            assert metadata is not None
            return self.add(
                self._new_id(), metadata["name"], metadata["parents"][0], mime=mime, content=content
            )
        self.content[file_id] = content
        self.files[file_id]["modifiedTime"] = "2026-03-01T00:00:00Z"
        return self.files[file_id]

    def client(self) -> GoogleDriveClient:
        transport = httpx.MockTransport(self.handler)
        return GoogleDriveClient(FakeCredential(), http=httpx.AsyncClient(transport=transport))


def config(**options: Any) -> ConnectorConfig:
    return ConnectorConfig(
        provider="google_drive", name="reports", options={"folder": ROOT, **options}
    )


async def connected(drive: FakeDrive, **options: Any) -> GoogleDriveConnector:
    connector = GoogleDriveConnector(config(**options))
    connector._client = drive.client()
    await connector._bind_root()
    return connector


async def stored(
    drive: FakeDrive, tmp_path: Path, **options: Any
) -> tuple[GoogleDriveConnector, Store]:
    store = await init_store(StoreConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'drive.db'}"))
    connector = await init_connector(config(**options), store)
    assert isinstance(connector, GoogleDriveConnector)
    connector._client = drive.client()
    await connector._bind_root()
    return connector, store


async def collect(changes: AsyncIterator[Change]) -> list[Change]:
    return [change async for change in changes]


async def background(connector: GoogleDriveConnector) -> list[Change]:
    seen: list[Change] = []

    async def on_change(change: Change) -> None:
        seen.append(change)

    await connector.sync_in_background(on_change)
    return seen


def summary(changes: list[Change]) -> list[tuple[str, ChangeType]]:
    return [(change.item_id, change.type) for change in changes]


def _service_account_info() -> dict[str, Any]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return {
        "type": "service_account",
        "client_email": "reader@project.iam.gserviceaccount.com",
        "private_key": pem,
        "private_key_id": "key-1",
        "token_uri": TOKEN_URL,
    }


class TestAuth:
    async def test_refresh_token_credential_caches_until_expiry(self) -> None:
        posted: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            posted.append(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(
                200, json={"access_token": f"token-{len(posted)}", "expires_in": 3600}
            )

        credential = RefreshTokenCredential(
            "client",
            "secret",
            "refresh",
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        assert await credential.get_token() == "token-1"
        assert await credential.get_token() == "token-1"
        assert posted[0]["grant_type"] == "refresh_token"
        assert posted[0]["refresh_token"] == "refresh"

        credential._expires_at = time.time()
        assert await credential.get_token() == "token-2"

    async def test_service_account_signs_a_jwt_grant_with_subject(self) -> None:
        info = _service_account_info()
        posted: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            posted.append(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(200, json={"access_token": "sa-token", "expires_in": 3600})

        credential = ServiceAccountCredential(
            info,
            subject="person@example.com",
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        assert await credential.get_token() == "sa-token"
        assert posted[0]["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
        claims = jwt.decode(posted[0]["assertion"], verify=False)  # type: ignore[no-untyped-call]
        assert claims["iss"] == info["client_email"]
        assert claims["sub"] == "person@example.com"
        assert claims["scope"] == "https://www.googleapis.com/auth/drive.readonly"

    async def test_refused_token_raises_authentication_error(self) -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(400, json={"error": "invalid_grant"})
        )
        credential = RefreshTokenCredential(
            "client", "secret", "revoked", http=httpx.AsyncClient(transport=transport)
        )

        with pytest.raises(AuthenticationError):
            await credential.get_token()

    def test_config_builds_each_credential_type(self) -> None:
        oauth = credential_from_config(
            AuthConfig(
                type="oauth",
                credentials={"client_id": "c", "client_secret": "s", "refresh_token": "r"},
            )
        )
        service_account = credential_from_config(
            AuthConfig(
                type="service_account",
                credentials={"service_account_info": json.dumps(_service_account_info())},
            )
        )

        assert isinstance(oauth, RefreshTokenCredential)
        assert isinstance(service_account, ServiceAccountCredential)

    @pytest.mark.parametrize(
        "auth",
        [
            None,
            AuthConfig(type="oauth", credentials={"client_id": "c"}),
            AuthConfig(type="service_account", credentials={}),
            AuthConfig(type="service_account", credentials={"service_account_info": "{nope"}),
            AuthConfig(
                type="service_account",
                credentials={"service_account_info": {"client_email": "x", "private_key": "bad"}},
            ),
            AuthConfig(type="api_key", credentials={}),
        ],
    )
    def test_config_problems_raise_configuration_error(self, auth: AuthConfig | None) -> None:
        with pytest.raises(ConfigurationError):
            credential_from_config(auth)


class TestConstruction:
    def test_requires_folder_option(self) -> None:
        with pytest.raises(ConfigurationError):
            GoogleDriveConnector(ConnectorConfig(provider="google_drive"))

    def test_refuses_a_folder_name(self) -> None:
        with pytest.raises(ConfigurationError):
            GoogleDriveConnector(config(folder="Reports"))

    def test_parses_folder_urls_ids_and_root(self) -> None:
        url = "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp?usp=sharing"

        assert parse_folder(url) == "1AbCdEfGhIjKlMnOp"
        assert parse_folder("1AbCdEfGhIjKlMnOp") == "1AbCdEfGhIjKlMnOp"
        assert parse_folder("root") == "root"
        assert parse_folder("My Reports") is None

    async def test_connect_resolves_the_root_alias_and_shared_drive(self) -> None:
        drive = FakeDrive()
        drive.files["myroot"]["driveId"] = "shared-1"

        connector = await connected(drive, folder="root")

        assert connector._root_id == "myroot"
        assert connector._drive_id == "shared-1"
        assert await connector.validate() is True

    async def test_connect_refuses_a_file_as_root(self) -> None:
        drive = FakeDrive()
        drive.add("file1aaaaaaaaaaa", "a.pdf", ROOT)

        with pytest.raises(ConfigurationError):
            await connected(drive, folder="file1aaaaaaaaaaa")

    async def test_validate_is_false_once_the_root_is_trashed(self) -> None:
        drive = FakeDrive()
        connector = await connected(drive)
        drive.files[ROOT]["trashed"] = True

        assert await connector.validate() is False


def _tree(drive: FakeDrive) -> None:
    drive.add("a", "a.pdf", ROOT, content=b"A")
    drive.add("sub", "Q1", ROOT, folder=True)
    drive.add("b", "b.pdf", "sub", content=b"B")
    drive.add("doc", "Notes", "sub", mime=DOC)
    drive.add("form", "Survey", ROOT, mime=FORM)
    drive.add("short", "link", ROOT, mime=SHORTCUT_MIME)
    drive.add("arch", "Archive", ROOT, folder=True)
    drive.add("old", "old.pdf", "arch")
    drive.add("outside", "secret.pdf", "myroot")


class TestList:
    async def test_recursive_lists_documents_with_relative_paths(self) -> None:
        drive = FakeDrive()
        _tree(drive)
        connector = await connected(drive, excluded_paths=["Archive"])

        items = {item.id: item async for item in connector.list(recursive=True)}

        assert set(items) == {"a", "b", "doc"}
        assert items["a"].path == ""
        assert items["b"].path == "Q1"
        assert items["a"].hashes[0].value == "md5-a"
        assert items["doc"].name == "Notes.pdf"
        assert items["doc"].mime_type == PDF
        assert items["doc"].metadata["google_mime_type"] == DOC

    async def test_one_level_includes_subfolders(self) -> None:
        drive = FakeDrive()
        _tree(drive)
        connector = await connected(drive, excluded_paths=["Archive"])

        items = {item.id: item async for item in connector.list()}

        assert set(items) == {"a", "sub"}
        assert items["sub"].type == ItemType.FOLDER

    async def test_path_resolves_a_nested_folder_by_name(self) -> None:
        drive = FakeDrive()
        _tree(drive)
        connector = await connected(drive)

        items = [item async for item in connector.list("Q1")]

        assert {item.id for item in items} == {"b", "doc"}
        assert {item.path for item in items} == {"Q1"}

    async def test_missing_or_ambiguous_paths_fail(self) -> None:
        drive = FakeDrive()
        _tree(drive)
        drive.add("sub2", "Q1", ROOT, folder=True)
        connector = await connected(drive)

        with pytest.raises(DataSourceError, match="ambiguous"):
            _ = [item async for item in connector.list("Q1")]
        with pytest.raises(NotFoundError):
            _ = [item async for item in connector.list("Q9")]
        with pytest.raises(NotFoundError):
            _ = [item async for item in connector.list("../x")]


async def all_pages(connector: GoogleDriveConnector, **kwargs: Any) -> list[list[Item]]:
    pages: list[list[Item]] = []
    token: str | None = None
    while True:
        page = await connector.list_page(page_token=token, **kwargs)
        pages.append(page.items)
        token = page.next_page_token
        if token is None:
            return pages


class TestListPage:
    async def test_one_level_pages_through_what_list_yields(self) -> None:
        drive = FakeDrive()
        _tree(drive)
        for n in range(5):
            drive.add(f"f{n}", f"file-{n}.pdf", ROOT)
        connector = await connected(drive, excluded_paths=["Archive"])

        pages = await all_pages(connector, page_size=2)

        assert all(len(page) <= 2 for page in pages)
        paged = [item.id for page in pages for item in page]
        assert sorted(paged) == sorted([item.id async for item in connector.list()])
        assert paged[0] == "sub"  # folders first

    async def test_recursive_pages_walk_the_subtree(self) -> None:
        drive = FakeDrive()
        _tree(drive)
        drive.add("deep", "Deep", "sub", folder=True)
        drive.add("d1", "d1.pdf", "deep")
        connector = await connected(drive, excluded_paths=["Archive"])

        pages = await all_pages(connector, recursive=True, page_size=1)

        items = {item.id: item for page in pages for item in page}
        assert set(items) == {"a", "b", "doc", "d1"}
        assert items["d1"].path == "Q1/Deep"

    async def test_search_matches_names_case_insensitively(self) -> None:
        drive = FakeDrive()
        _tree(drive)
        drive.add("inv1", "Invoice-March.pdf", "sub")
        drive.add("inv2", "old invoice.pdf", ROOT)
        connector = await connected(drive)

        pages = await all_pages(connector, recursive=True, page_size=1, search="INVOICE")

        assert sorted(item.id for page in pages for item in page) == ["inv1", "inv2"]

    async def test_a_token_only_resumes_its_own_listing(self) -> None:
        drive = FakeDrive()
        _tree(drive)
        connector = await connected(drive)
        page = await connector.list_page(page_size=1)
        assert page.next_page_token is not None

        with pytest.raises(InvalidPageTokenError):
            await connector.list_page("Q1", page_size=1, page_token=page.next_page_token)
        with pytest.raises(InvalidPageTokenError):
            await connector.list_page(recursive=True, page_size=1, page_token=page.next_page_token)
        with pytest.raises(InvalidPageTokenError):
            await connector.list_page(page_token="not-a-token")

    async def test_a_forged_token_cannot_list_outside(self) -> None:
        drive = FakeDrive()
        _tree(drive)
        connector = await connected(drive)
        forged = encode_token(
            {
                "kind": "google_drive",
                "start": ROOT,
                "recursive": True,
                "folder": "myroot",
                "path": "",
                "drive_token": None,
                "queue": [["myroot", ""]],
            }
        )

        page = await connector.list_page(recursive=True, page_token=forged)

        assert page.items == [] and page.next_page_token is None

    async def test_a_folder_moved_out_between_pages_is_dropped(self) -> None:
        drive = FakeDrive()
        _tree(drive)
        connector = await connected(drive, excluded_paths=["Archive"])
        first = await connector.list_page(recursive=True, page_size=1)
        assert [item.id for item in first.items] == ["a"]  # Q1 is queued, not yet read
        assert first.next_page_token is not None

        drive.files["sub"]["parents"] = ["myroot"]
        rest = await connector.list_page(recursive=True, page_token=first.next_page_token)

        assert rest.items == [] and rest.next_page_token is None

    async def test_page_size_must_be_positive(self) -> None:
        connector = await connected(FakeDrive())

        with pytest.raises(ValueError):
            await connector.list_page(page_size=0)


class TestBoundary:
    async def test_get_metadata_places_an_item_inside(self) -> None:
        drive = FakeDrive()
        _tree(drive)
        connector = await connected(drive)

        item = await connector.get_metadata("b")

        assert item.path == "Q1"
        assert item.parent_id == "sub"

    @pytest.mark.parametrize("item_id", ["outside", "short", "old", "missing"])
    async def test_get_metadata_refuses_what_is_not_inside(self, item_id: str) -> None:
        drive = FakeDrive()
        _tree(drive)
        connector = await connected(drive, excluded_paths=["Archive"])

        with pytest.raises(NotFoundError):
            await connector.get_metadata(item_id)

    async def test_get_metadata_refuses_a_trashed_file(self) -> None:
        drive = FakeDrive()
        _tree(drive)
        drive.files["a"]["trashed"] = True
        connector = await connected(drive)

        with pytest.raises(NotFoundError):
            await connector.get_metadata("a")

    async def test_download_streams_a_file_and_exports_a_google_doc(self) -> None:
        drive = FakeDrive()
        _tree(drive)
        connector = await connected(drive)

        plain = b"".join(
            [chunk async for chunk in connector.download(await connector.get_metadata("b"))]
        )
        doc = b"".join(
            [chunk async for chunk in connector.download(await connector.get_metadata("doc"))]
        )

        assert plain == b"B"
        assert doc == f"exported:{PDF}".encode()

    async def test_download_refuses_a_built_item_outside_and_a_form(self) -> None:
        drive = FakeDrive()
        _tree(drive)
        connector = await connected(drive)

        with pytest.raises(NotFoundError):
            _ = [
                chunk
                async for chunk in connector.download(
                    Item(id="outside", name="secret.pdf", type=ItemType.FILE)
                )
            ]
        with pytest.raises(UnsupportedOperationError):
            _ = [
                chunk
                async for chunk in connector.download(
                    Item(id="form", name="Survey", type=ItemType.FILE)
                )
            ]

    async def test_export_formats_override(self) -> None:
        drive = FakeDrive()
        _tree(drive)
        docx = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        connector = await connected(
            drive, export_formats={DOC: {"mime_type": docx, "extension": "docx"}}
        )

        item = await connector.get_metadata("doc")

        assert item.name == "Notes.docx"
        assert item.mime_type == docx

    def test_export_formats_must_name_a_type_and_extension(self) -> None:
        with pytest.raises(ConfigurationError):
            GoogleDriveConnector(config(export_formats={DOC: "application/pdf"}))


class TestWrite:
    async def test_upload_creates_a_file_in_a_folder(self) -> None:
        drive = FakeDrive()
        _tree(drive)
        connector = await connected(drive)

        item = await connector.upload("Q1", "report.pdf", b"PDF")

        assert item.name == "report.pdf" and item.path == "Q1" and item.mime_type == PDF
        assert drive.content[item.id] == b"PDF"
        assert drive.files[item.id]["parents"] == ["sub"]
        assert drive.uploads == ["multipart"]

    async def test_upload_creates_missing_folders_along_the_way(self) -> None:
        drive = FakeDrive()
        _tree(drive)
        connector = await connected(drive)

        item = await connector.upload("Out/2026/Oct", "a.csv", b"x,y", mime_type="text/csv")

        assert item.path == "Out/2026/Oct" and item.mime_type == "text/csv"
        assert (await connector.get_metadata(item.id)).path == "Out/2026/Oct"
        again = await connector.upload("Out/2026/Oct", "b.csv", b"1")
        assert again.parent_id == item.parent_id  # the folders made once are reused

    async def test_a_taken_name_fails_replaces_or_renames(self) -> None:
        drive = FakeDrive()
        _tree(drive)
        connector = await connected(drive)

        with pytest.raises(ConflictError):
            await connector.upload("", "a.pdf", b"new")

        replaced = await connector.upload("", "a.pdf", b"new", on_conflict=ConflictBehavior.REPLACE)
        assert replaced.id == "a" and drive.content["a"] == b"new"
        assert drive.uploads[-1] == "media"

        first = await connector.upload("", "a.pdf", b"1", on_conflict=ConflictBehavior.RENAME)
        second = await connector.upload("", "a.pdf", b"2", on_conflict=ConflictBehavior.RENAME)
        assert (first.name, second.name) == ("a (1).pdf", "a (2).pdf")

    async def test_a_google_doc_or_a_folder_is_never_written_over(self) -> None:
        drive = FakeDrive()
        _tree(drive)
        connector = await connected(drive)

        with pytest.raises(ConflictError):
            await connector.upload("Q1", "Notes", b"x", on_conflict=ConflictBehavior.REPLACE)
        with pytest.raises(ConflictError):
            await connector.upload("", "Q1", b"x", on_conflict=ConflictBehavior.REPLACE)
        beside = await connector.upload("", "Q1", b"x", on_conflict=ConflictBehavior.RENAME)
        assert beside.name == "Q1 (1)"

    async def test_large_content_goes_up_in_a_resumable_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(drive_client, "SIMPLE_UPLOAD_LIMIT", 10)
        monkeypatch.setattr(drive_client, "CHUNK_SIZE", 4)
        drive = FakeDrive()
        _tree(drive)
        connector = await connected(drive)

        created = await connector.upload("", "big.bin", b"0123456789AB")
        replaced = await connector.upload(
            "", "a.pdf", b"abcdefghijk", on_conflict=ConflictBehavior.REPLACE
        )

        assert drive.content[created.id] == b"0123456789AB"
        assert drive.content[replaced.id] == b"abcdefghijk"
        assert drive.uploads == ["resumable", "chunk", "chunk", "chunk"] * 2

    async def test_writes_may_go_into_an_excluded_folder(self) -> None:
        drive = FakeDrive()
        _tree(drive)
        connector = await connected(drive, excluded_paths=["Archive"])

        item = await connector.upload("Archive", "out.pdf", b"x")
        await connector.delete(item.id)

        assert drive.files[item.id]["trashed"] is True

    async def test_a_name_or_folder_that_would_lead_elsewhere_is_refused(self) -> None:
        connector = await connected(FakeDrive())

        with pytest.raises(ValueError):
            await connector.upload("", "sub/escape.pdf", b"x")
        with pytest.raises(NotFoundError):
            await connector.upload("../elsewhere", "a.pdf", b"x")

    async def test_create_folder_returns_an_existing_one_and_refuses_ambiguity(self) -> None:
        drive = FakeDrive()
        _tree(drive)
        connector = await connected(drive)

        assert (await connector.create_folder("Q1")).id == "sub"
        made = await connector.create_folder("Q1/New")
        assert made.type == ItemType.FOLDER and made.path == "Q1"
        drive.add("sub2", "Q1", ROOT, folder=True)
        with pytest.raises(DataSourceError, match="ambiguous"):
            await connector.create_folder("Q1/Other")

    async def test_delete_trashes_unless_permanent(self) -> None:
        drive = FakeDrive()
        _tree(drive)
        connector = await connected(drive)

        await connector.delete("a")
        await connector.delete("b", permanent=True)

        assert drive.files["a"]["trashed"] is True
        assert "b" not in drive.files

    async def test_delete_refuses_the_root_and_what_is_outside(self) -> None:
        drive = FakeDrive()
        _tree(drive)
        connector = await connected(drive)

        with pytest.raises(UnsupportedOperationError):
            await connector.delete(ROOT)
        with pytest.raises(NotFoundError):
            await connector.delete("outside")
        assert "outside" in drive.files and not drive.files["outside"]["trashed"]


class TestSync:
    async def test_first_sync_walks_the_folder_and_commits_the_start_token(
        self, tmp_path: Path
    ) -> None:
        drive = FakeDrive()
        _tree(drive)
        drive.record("outside")
        connector, store = await stored(drive, tmp_path, excluded_paths=["Archive"])
        try:
            changes = await background(connector)

            assert sorted(summary(changes)) == [
                ("a", ChangeType.CREATED),
                ("b", ChangeType.CREATED),
                ("doc", ChangeType.CREATED),
            ]
            assert [c.cursor for c in changes[:-1]] == [None, None]
            assert changes[-1].cursor == SyncCursor(token="1")
            assert await connector._load_cursor() == SyncCursor(token="1")
            indexed = await connector.get_item("b")
            assert indexed is not None and indexed.path == "Q1"
        finally:
            await store.close()

    async def test_later_syncs_follow_file_changes_inside_the_folder(self, tmp_path: Path) -> None:
        drive = FakeDrive()
        _tree(drive)
        drive.add("unseen", "unseen.pdf", "myroot")
        connector, store = await stored(drive, tmp_path, excluded_paths=["Archive"])
        try:
            await background(connector)

            drive.create("new", "new.pdf", "sub")
            drive.edit("a")
            drive.edit("outside")
            drive.create("archived", "x.pdf", "arch")
            drive.trash("b")
            drive.move("doc", "myroot")
            drive.record("unseen", removed=True)

            changes = await background(connector)

            assert summary(changes) == [
                ("new", ChangeType.CREATED),
                ("a", ChangeType.UPDATED),
                ("b", ChangeType.DELETED),
                ("doc", ChangeType.DELETED),
            ]
            assert await connector.get_item("b") is None
            assert await connector._load_cursor() == SyncCursor(token=str(len(drive.log)))
        finally:
            await store.close()

    async def test_cursor_moves_past_changes_that_are_all_outside(self, tmp_path: Path) -> None:
        drive = FakeDrive()
        _tree(drive)
        connector, store = await stored(drive, tmp_path)
        try:
            await background(connector)
            drive.edit("outside")
            drive.edit("outside")

            assert await background(connector) == []
            assert await connector._load_cursor() == SyncCursor(token=str(len(drive.log)))
        finally:
            await store.close()

    async def test_cursor_rides_the_last_change_of_each_page(self, tmp_path: Path) -> None:
        drive = FakeDrive()
        _tree(drive)
        connector, store = await stored(drive, tmp_path)
        try:
            await background(connector)
            drive.page_size = 2
            start = len(drive.log)
            for file_id in ("a", "b", "outside", "a"):
                drive.edit(file_id)

            changes = await background(connector)

            assert [c.item_id for c in changes] == ["a", "b", "a"]
            assert [c.cursor for c in changes] == [
                None,
                SyncCursor(token=str(start + 2)),
                SyncCursor(token=str(start + 4)),
            ]
        finally:
            await store.close()

    async def test_a_folder_moved_in_is_walked(self, tmp_path: Path) -> None:
        drive = FakeDrive()
        _tree(drive)
        drive.add("ext", "Incoming", "myroot", folder=True)
        drive.add("e1", "e1.pdf", "ext")
        drive.add("deep", "Deep", "ext", folder=True)
        drive.add("e2", "e2.pdf", "deep")
        connector, store = await stored(drive, tmp_path)
        try:
            await background(connector)
            drive.move("ext", ROOT)

            changes = await background(connector)

            assert sorted(summary(changes)) == [
                ("e1", ChangeType.CREATED),
                ("e2", ChangeType.CREATED),
            ]
            e2 = await connector.get_item("e2")
            assert e2 is not None and e2.path == "Incoming/Deep"
        finally:
            await store.close()

    async def test_a_folder_moved_out_has_its_files_deleted(self, tmp_path: Path) -> None:
        drive = FakeDrive()
        _tree(drive)
        drive.add("deep", "Deep", "sub", folder=True)
        drive.add("d1", "d1.pdf", "deep")
        connector, store = await stored(drive, tmp_path, excluded_paths=["Archive"])
        try:
            await background(connector)
            drive.move("sub", "myroot")

            changes = await background(connector)

            assert sorted(summary(changes)) == [
                ("b", ChangeType.DELETED),
                ("d1", ChangeType.DELETED),
                ("doc", ChangeType.DELETED),
            ]
            # Moving it back in is seen as new again — its records went with it.
            drive.move("sub", ROOT)
            again = await background(connector)
            assert sorted(summary(again)) == [
                ("b", ChangeType.CREATED),
                ("d1", ChangeType.CREATED),
                ("doc", ChangeType.CREATED),
            ]
        finally:
            await store.close()

    async def test_a_renamed_folder_updates_its_files_paths(self, tmp_path: Path) -> None:
        drive = FakeDrive()
        _tree(drive)
        connector, store = await stored(drive, tmp_path, excluded_paths=["Archive"])
        try:
            await background(connector)
            drive.rename("sub", "Q1-final")

            changes = await background(connector)

            assert sorted(summary(changes)) == [
                ("b", ChangeType.UPDATED),
                ("doc", ChangeType.UPDATED),
            ]
            b = await connector.get_item("b")
            assert b is not None and b.path == "Q1-final"

            drive.edit("sub")  # a folder change that moves nothing is not a change of its files
            assert await background(connector) == []
        finally:
            await store.close()

    async def test_without_a_store_only_file_changes_are_followed(self) -> None:
        drive = FakeDrive()
        _tree(drive)
        connector = await connected(drive)
        first = await collect(connector.sync())
        cursor = first[-1].cursor
        drive.create("new", "new.pdf", ROOT)
        drive.edit("a")
        drive.trash("b")

        changes = await collect(connector.sync(cursor))

        assert summary(changes) == [("new", ChangeType.CREATED), ("a", ChangeType.UPDATED)]

    async def test_a_shared_drive_reads_its_own_change_feed(self, tmp_path: Path) -> None:
        drive = FakeDrive()
        drive.files[ROOT]["driveId"] = "shared-1"
        connector, store = await stored(drive, tmp_path)
        try:
            await background(connector)
            await background(connector)

            feed = [r for r in drive.requests if r.url.path.endswith("/changes")]
            assert feed and all(r.url.params["driveId"] == "shared-1" for r in feed)
        finally:
            await store.close()


class TestWebhooks:
    async def test_create_opens_a_channel_and_verifies_its_notifications(
        self, tmp_path: Path
    ) -> None:
        drive = FakeDrive()
        connector, store = await stored(drive, tmp_path)
        try:
            subscription = await connector.create_webhook("https://example.com/hooks/drive")

            watched = drive.watched[0]
            assert watched["address"] == "https://example.com/hooks/drive"
            assert watched["type"] == "web_hook"
            assert watched["token"] == subscription.client_state
            assert subscription.id == watched["id"]
            assert subscription.resource == "resource-1"

            headers = {
                "X-Goog-Channel-ID": subscription.id,
                "X-Goog-Channel-Token": subscription.client_state or "",
                "X-Goog-Resource-State": "change",
            }
            assert await connector.verify_webhook_notification(headers) is True
            tampered = {**headers, "X-Goog-Channel-Token": "guess"}
            assert await connector.verify_webhook_notification(tampered) is False
            assert await connector.list_webhooks() == [subscription]
        finally:
            await store.close()

    async def test_verify_rejects_before_any_channel(self, tmp_path: Path) -> None:
        connector, store = await stored(FakeDrive(), tmp_path)
        try:
            assert await connector.verify_webhook_notification({"x-goog-channel-id": "x"}) is False
            assert await connector.list_webhooks() == []
        finally:
            await store.close()

    async def test_renew_opens_a_new_channel_then_stops_the_old(self, tmp_path: Path) -> None:
        drive = FakeDrive()
        connector, store = await stored(drive, tmp_path)
        try:
            old = await connector.create_webhook("https://example.com/hooks/drive")

            new = await connector.renew_webhook(old.id)

            assert new.id != old.id
            assert new.notification_url == old.notification_url
            assert drive.stopped == [{"id": old.id, "resourceId": "resource-1"}]
            assert await connector.list_webhooks() == [new]
        finally:
            await store.close()

    async def test_delete_stops_the_channel_and_forgets_it(self, tmp_path: Path) -> None:
        drive = FakeDrive()
        connector, store = await stored(drive, tmp_path)
        try:
            subscription = await connector.create_webhook("https://example.com/hooks/drive")

            await connector.delete_webhook(subscription.id)

            assert drive.stopped == [{"id": subscription.id, "resourceId": "resource-1"}]
            assert await connector.list_webhooks() == []
            with pytest.raises(NotFoundError):
                await connector.delete_webhook(subscription.id)
        finally:
            await store.close()


class TestPermissions:
    async def test_maps_drive_roles_and_principals(self) -> None:
        drive = FakeDrive()
        _tree(drive)
        drive.permissions["a"] = [
            {"type": "user", "role": "owner", "emailAddress": "owner@example.com"},
            {"type": "group", "role": "fileOrganizer", "emailAddress": "team@example.com"},
            {"type": "domain", "role": "reader", "domain": "example.com"},
            {"type": "anyone", "role": "commenter"},
            {"type": "user", "role": "unknownRole", "emailAddress": "x@example.com"},
        ]
        connector = await connected(drive)

        permissions = await connector.get_permissions(await connector.get_metadata("a"))

        assert [(p.principal, p.principal_type, p.role) for p in permissions] == [
            ("owner@example.com", PermissionPrincipalType.USER, PermissionRole.OWNER),
            ("team@example.com", PermissionPrincipalType.GROUP, PermissionRole.WRITER),
            ("example.com", PermissionPrincipalType.DOMAIN, PermissionRole.READER),
            ("anyone", PermissionPrincipalType.ANYONE, PermissionRole.COMMENTER),
        ]

    async def test_refuses_an_item_outside(self) -> None:
        drive = FakeDrive()
        _tree(drive)
        connector = await connected(drive)

        with pytest.raises(NotFoundError):
            await connector.get_permissions(Item(id="outside", name="x", type=ItemType.FILE))


def _client(handler: Any) -> GoogleDriveClient:
    return GoogleDriveClient(
        FakeCredential(), http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


class TestClient:
    async def test_retries_throttling_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("asyncio.sleep", fake_sleep)
        responses = [
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(403, json={"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}),
            httpx.Response(503),
            httpx.Response(200, json={"id": "x"}),
        ]
        client = _client(lambda request: responses.pop(0))

        assert await client.get_file("x") == {"id": "x"}
        assert sleeps == [3.0, 2.0, 4.0]

    async def test_gives_up_after_max_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_sleep(seconds: float) -> None:
            return None

        monkeypatch.setattr("asyncio.sleep", fake_sleep)
        client = _client(lambda request: httpx.Response(429))

        with pytest.raises(RateLimitError):
            await client.request("GET", "files/x", max_retries=2)

    @pytest.mark.parametrize(
        ("status", "body", "error"),
        [
            (401, {}, AuthenticationError),
            (
                403,
                {"error": {"errors": [{"reason": "insufficientPermissions"}]}},
                AuthenticationError,
            ),
            (404, {}, NotFoundError),
            (400, {}, DataSourceError),
        ],
    )
    async def test_maps_errors(
        self, status: int, body: dict[str, Any], error: type[Exception]
    ) -> None:
        client = _client(lambda request: httpx.Response(status, json=body))

        with pytest.raises(error):
            await client.get_file("x")

    async def test_sends_the_bearer_token_and_closes_the_credential(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"id": "x"})

        credential = FakeCredential()
        client = GoogleDriveClient(
            credential, http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )

        await client.get_file("x")
        await client.close()

        assert seen[0].headers["Authorization"] == "Bearer fake-token"
        assert seen[0].url.params["supportsAllDrives"] == "true"
        assert credential.closed
