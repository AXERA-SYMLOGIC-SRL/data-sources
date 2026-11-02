"""Odoo (16 and later, Community or Enterprise, self-hosted, Odoo.sh or Odoo Online), read-only.

Connection (`auth.credentials`): `url` (the instance), `database`, `api_key` (a user's API key:
avatar → My Profile → Account Security → New API Key) and, optionally, `username` (that user's
login). Odoo 19 and later are read through the JSON-2 API (`POST /json/2/<model>/<method>`, the
key as a bearer token, the database in `X-Odoo-Database`); older versions through `execute_kw`
over JSON-RPC, which needs the login, and which Odoo removes in version 22 (Odoo Online 21.1).
On 19 and later, a key JSON-2 refuses is tried once over JSON-RPC when a login is given: there
the secret may also be the user's password (Odoo's own demo databases have no key). The user's
access rights apply: a model they can't read isn't `available`. Odoo Online only opens the
external API on its Custom plan.

What is read is a spec in Odoo's terms:

    {"model": "sale.order",            # the model
     "domain": [["state", "!=", "cancel"]],   # optional, Odoo's domain
     "extra": ["list_price"],          # optional: non-stored fields to read too, when they exist
     "fields": ["name", "login"]}      # optional: exactly these fields instead of every stored one

A record comes back as Odoo answers `search_read`: a many2one is `[id, "display name"]`, a
many2many a list of ids, and every empty value is `false`. Without `fields`, every stored field
the user may read is asked for (custom `x_` fields included) except one2many lists, binaries,
HTML bodies, properties, the chatter and activity widgets, and credential-like names
(`SECRET_WORDS`). Every read includes archived records (`active_test: False`) and every company
the user may access, in English (`lang: en_US`), oldest change (`write_date`) first.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit

import httpx

from data_sources.erp.base import (
    PAGE_SIZE,
    SECRET_WORDS,
    Company,
    Cursor,
    ErpRecord,
    ErpSource,
    NativeField,
    Page,
    SourceInfo,
    Spec,
)
from data_sources.reports.errors import Problem, ReportError
from data_sources.reports.http import ApiClient, Retry, Verdict, exponential

UNREACHABLE = Problem("unreachable", "Odoo could not be reached at that URL.")
BUSY = Problem("unavailable", "Odoo is unavailable or overloaded; try again.")
INVALID = Problem("unavailable", "Odoo returned an invalid response.")
AUTH_FAILED = Problem(
    "auth_failed", "Odoo refused the API key (wrong, expired or revoked) or the login."
)
NO_DATABASE = Problem("database_not_found", "Odoo has no database by that name at this URL.")
REFUSED = Problem("request_failed", "Odoo refused the request.")
NO_USERNAME = Problem("request_failed", "Odoo before version 19 also needs the user's login.")
NO_ACCESS = Problem("access_denied", "The Odoo user may not read this.")

#: Field types never read: lists of child ids, files, HTML bodies, property bags.
SKIPPED_TYPES = {"one2many", "binary", "html", "properties", "properties_definition"}
#: Field-name prefixes never read: the chatter, activities, ratings, website messages, and
#: Odoo's magic fields.
SKIPPED_PREFIXES = ("message_", "activity_", "website_message", "rating_", "__")
FIELD_ATTRIBUTES = ["string", "type", "relation", "store", "selection", "help"]
#: The first major version read through JSON-2.
JSON2_FROM = 19


def _error_parts(body: Any) -> tuple[str, str]:
    """`(exception name, message)` of an Odoo error: JSON-2's body, or JSON-RPC's `error.data`.
    Never the `debug` traceback (it can hold anything)."""
    if not isinstance(body, dict):
        return "", ""
    inner = body.get("data")
    data: dict[str, Any] = inner if isinstance(inner, dict) else body
    return str(data.get("name") or ""), str(data.get("message") or "")[:300]


def _problem(name: str, message: str) -> Problem:
    """What an Odoo exception means, by its name."""
    diagnostics = f"{name}: {message}" if name else message
    if "AccessDenied" in name or "SessionExpired" in name:
        return AUTH_FAILED
    if "AccessError" in name:
        return Problem(NO_ACCESS.reason, NO_ACCESS.message, diagnostics)
    lowered = message.lower()
    if "KeyError" in name or ("OperationalError" in name and "database" in lowered):
        return NO_DATABASE
    return Problem(REFUSED.reason, REFUSED.message, diagnostics)


def classify(response: httpx.Response) -> Verdict:
    """JSON-2 answers with HTTP statuses (an invalid key is a 401, a forbidden model a 403 naming
    `AccessError`, an unknown model a 404 JSON error, an unknown database a 404 HTML page);
    JSON-RPC answers 200 with an `error` object."""
    if response.status_code == 429 or response.status_code >= 500:
        return Retry(BUSY)
    is_json = response.headers.get("content-type", "").startswith("application/json")
    body: Any = None
    if is_json:
        try:
            body = response.json()
        except ValueError:
            return INVALID
    if response.status_code == 401:
        return AUTH_FAILED
    if response.status_code == 403:
        name, message = _error_parts(body)
        return _problem(name, message) if "AccessError" in name else AUTH_FAILED
    if response.status_code == 404:
        return _problem(*_error_parts(body)) if is_json else NO_DATABASE
    if response.is_error:
        return _problem(*_error_parts(body))
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return _problem(*_error_parts(body["error"]))
    return None


def parse_datetime(value: Any) -> dt.datetime | None:
    """Odoo's `YYYY-MM-DD HH:MM:SS` (UTC)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.UTC)
    except ValueError:
        return None


def readable(name: str, spec: Mapping[str, Any]) -> bool:
    """Whether a field is read when no explicit `fields` are asked for."""
    return (
        bool(spec.get("store"))
        and spec.get("type") not in SKIPPED_TYPES
        and not name.startswith(SKIPPED_PREFIXES)
        and not any(word in name for word in SECRET_WORDS)
    )


class OdooSource(ErpSource):
    provider = "odoo"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._client: ApiClient | None = None
        self._entered = 0
        self._version: str = ""
        self._api: str | None = None
        self._uid: int | None = None
        self._companies: list[int] | None = None
        self._fields: dict[str, dict[str, dict[str, Any]]] = {}

    @property
    def base_url(self) -> str:
        """The instance's root. People paste what their browser shows (`.../odoo/action-12`,
        `.../web#menu_id=3`, `.../web/login`): Odoo's own pages start at `/odoo` or `/web`, so the
        address is cut there, and a query or fragment goes too. A sub-path before them is kept."""
        url = self.credential("url")
        parsed = urlsplit(url if url.startswith(("http://", "https://")) else f"https://{url}")
        path = re.split(r"/(?:odoo|web)(?:/|$)", parsed.path, maxsplit=1)[0].rstrip("/")
        return f"{parsed.scheme}://{parsed.netloc}{path}"

    # The connection: one HTTP client for as long as the source is entered (nested entries share
    # it; each public method enters, so it works without an outer `async with` too).

    async def __aenter__(self) -> Self:
        self._entered += 1
        if self._client is None:
            client = ApiClient(
                classify,
                unreachable=UNREACHABLE,
                invalid=INVALID,
                base_url=self.base_url,
                # A multi-database server picks the database by this header (JSON-2 needs it;
                # older versions ignore it and get it from each call).
                headers={"X-Odoo-Database": self.credential("database")},
                timeout=120,
                attempts=4,
                backoff=exponential(1.0),
                transport=self.transport,
                sleep=self.sleep,
            )
            await client.__aenter__()
            self._client = client
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._entered -= 1
        if self._entered == 0 and self._client is not None:
            client, self._client = self._client, None
            await client.__aexit__(exc_type, exc, tb)

    async def _body(self, method: str, url: str, **kwargs: Any) -> Any:
        assert self._client is not None
        response = await self._client.request(method, url, retry=True, **kwargs)
        try:
            return response.json()
        except ValueError:
            raise INVALID.error() from None

    async def _jsonrpc(self, service: str, method: str, *args: Any) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": args},
        }
        body = await self._body("POST", "/jsonrpc", json=payload)
        if not isinstance(body, dict) or "result" not in body:
            raise INVALID.error()
        return body["result"]

    async def _detect(self) -> None:
        """The server's version and the API to read it with (`json2` from Odoo 19)."""
        body = await self._body(
            "POST",
            "/web/webclient/version_info",
            json={"jsonrpc": "2.0", "method": "call", "params": {}},
        )
        info = body.get("result") if isinstance(body, dict) else None
        if not isinstance(info, dict):
            raise INVALID.error()
        self._version = str(info.get("server_version", ""))
        major = (info.get("server_version_info") or [0])[0]
        self._api = "json2" if isinstance(major, int) and major >= JSON2_FROM else "jsonrpc"

    async def _login(self) -> None:
        """JSON-RPC's user id, from the login and the key (or password)."""
        username = self.credential("username")
        if not username:
            raise NO_USERNAME.error()
        uid = await self._jsonrpc(
            "common",
            "authenticate",
            self.credential("database"),
            username,
            self.credential("api_key"),
            {},
        )
        if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
            raise AUTH_FAILED.error()
        self._uid = uid

    async def _ready(self) -> None:
        """Connected and signed in: the API picked (JSON-2 refusing the key falls back to
        JSON-RPC when a login is given), the user known, the companies the user may access
        loaded."""
        if self._api is None:
            await self._detect()
        if self._api == "json2" and self._uid is None:
            try:
                context = await self._raw_call("res.users", "context_get")
            except ReportError as exc:
                if exc.reason != "auth_failed" or not self.credential("username"):
                    raise
                self._api = "jsonrpc"
            else:
                uid = context.get("uid") if isinstance(context, dict) else None
                if not isinstance(uid, int):
                    raise INVALID.error()
                self._uid = uid
        if self._api == "jsonrpc" and self._uid is None:
            await self._login()
        if self._companies is None:
            found = await self._raw_call("res.company", "search_read", domain=[], fields=["id"])
            if not isinstance(found, list):
                raise INVALID.error()
            self._companies = sorted(
                c["id"] for c in found if isinstance(c, dict) and isinstance(c.get("id"), int)
            )

    async def _raw_call(self, model: str, method: str, **kwargs: Any) -> Any:
        """`model.method(**kwargs)` as the user, over the API picked."""
        context: dict[str, Any] = {"active_test": False, "lang": "en_US"}
        if self._companies:
            context["allowed_company_ids"] = self._companies
        if self._api == "json2":
            return await self._body(
                "POST",
                f"/json/2/{model}/{method}",
                json={**kwargs, "context": context},
                headers={"Authorization": f"bearer {self.credential('api_key')}"},
            )
        return await self._jsonrpc(
            "object",
            "execute_kw",
            self.credential("database"),
            self._uid,
            self.credential("api_key"),
            model,
            method,
            [],
            {**kwargs, "context": context},
        )

    async def _call(self, model: str, method: str, **kwargs: Any) -> Any:
        async with self:
            await self._ready()
            return await self._raw_call(model, method, **kwargs)

    # What the instance is

    async def check(self) -> SourceInfo:
        if not self.configured:
            raise ReportError("not_configured", "Fill in the URL, the database and the API key.")
        async with self:
            await self._ready()
            users = await self._raw_call(
                "res.users",
                "search_read",
                domain=[["id", "=", self._uid]],
                fields=["name", "login"],
            )
            companies = await self._raw_call(
                "res.company", "search_read", domain=[], fields=["name", "currency_id"]
            )
        if not isinstance(users, list) or not users or not isinstance(companies, list):
            raise INVALID.error()
        return SourceInfo(
            version=self._version,
            api=self._api or "",
            user=str(users[0].get("login") or users[0].get("name")),
            companies=[
                Company(str(c["id"]), str(c.get("name") or ""), _display(c.get("currency_id")))
                for c in companies
            ],
        )

    async def available(self, specs: Mapping[str, Spec]) -> list[str]:
        """The specs whose model is installed and readable by the user."""
        async with self:
            await self._ready()
            found = await self._raw_call(
                "ir.model",
                "search_read",
                domain=[["model", "in", sorted({_model(s) for s in specs.values()})]],
                fields=["model"],
            )
            installed = {m["model"] for m in found if isinstance(m, dict)}
            readable_models: dict[str, bool] = {}
            for model in sorted(installed):
                try:
                    await self._raw_call(model, "search_count", domain=[["id", "=", 0]])
                except ReportError as exc:
                    if exc.reason != "access_denied":
                        raise
                    readable_models[model] = False
                else:
                    readable_models[model] = True
        return [name for name, spec in specs.items() if readable_models.get(_model(spec))]

    async def _model_fields(self, model: str) -> dict[str, dict[str, Any]]:
        if model not in self._fields:
            found = await self._raw_call(model, "fields_get", attributes=FIELD_ATTRIBUTES)
            if not isinstance(found, dict):
                raise INVALID.error()
            self._fields[model] = found
        return self._fields[model]

    async def fields(self, spec: Spec) -> list[NativeField]:
        async with self:
            await self._ready()
            found = await self._model_fields(_model(spec))
        return [
            NativeField(
                name,
                str(f.get("string") or name),
                str(f.get("type") or ""),
                f.get("relation") or None,
                bool(f.get("store")),
                tuple(
                    (str(pair[0]), str(pair[1]))
                    for pair in (f.get("selection") or [])
                    if isinstance(pair, list | tuple) and len(pair) == 2
                ),
                str(f.get("help") or ""),
            )
            for name, f in sorted(found.items())
        ]

    def read_fields(self, spec: Spec, fields: Mapping[str, Mapping[str, Any]]) -> list[str]:
        """The fields asked for: `spec["fields"]`, or every readable stored field and the `extra`
        ones; only those the model has, plus `write_date`."""
        if spec.get("fields"):
            wanted = {name for name in spec["fields"] if name in fields}
        else:
            wanted = {name for name, f in fields.items() if readable(name, f)}
            wanted |= {name for name in spec.get("extra") or () if name in fields}
        return sorted(wanted | {"write_date"})

    # Reading

    async def count(self, spec: Spec) -> int:
        found = await self._call(_model(spec), "search_count", domain=_domain(spec))
        return int(found)

    async def read(
        self, spec: Spec, cursor: Cursor | None = None, page_size: int = PAGE_SIZE
    ) -> Page:
        model = _model(spec)
        domain = _domain(spec)
        if cursor is not None:
            since = cursor.updated_at.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
            domain.append(["write_date", ">=", since])
        async with self:
            await self._ready()
            fields = await self._model_fields(model)
            rows = await self._raw_call(
                model,
                "search_read",
                domain=domain,
                fields=self.read_fields(spec, fields),
                offset=cursor.offset if cursor else 0,
                limit=page_size,
                order="write_date asc, id asc",
            )
        if not isinstance(rows, list):
            raise INVALID.error()
        records = [_record(model, row) for row in rows if isinstance(row, dict)]
        if len(rows) < page_size or not records:
            return Page(records, None)
        last = records[-1].updated_at
        if cursor is not None and last == cursor.updated_at:
            return Page(records, Cursor(last, cursor.offset + len(records)))
        return Page(records, Cursor(last, sum(1 for r in records if r.updated_at == last)))

    async def list_ids(self, spec: Spec) -> list[str]:
        found = await self._call(_model(spec), "search", domain=_domain(spec))
        if not isinstance(found, list):
            raise INVALID.error()
        return [str(i) for i in found]


def _model(spec: Spec) -> str:
    model = spec.get("model")
    if not isinstance(model, str) or not model:
        raise ReportError("request_failed", "An Odoo spec names its model.")
    return model


def _domain(spec: Spec) -> list[Any]:
    return [
        list(term) if isinstance(term, list | tuple) else term for term in spec.get("domain") or ()
    ]


def _display(value: Any) -> str:
    """The display name of a many2one (`[id, name]`)."""
    return str(value[1]) if isinstance(value, list) and len(value) == 2 else ""


def _record(model: str, row: dict[str, Any]) -> ErpRecord:
    updated = parse_datetime(row.get("write_date"))
    if updated is None:
        raise ReportError("unavailable", f"Odoo returned a {model} record without its change time.")
    return ErpRecord(str(row["id"]), updated, row)
