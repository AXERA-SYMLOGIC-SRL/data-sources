"""Microsoft Advertising (formerly Bing Ads), read-only, through its REST v13 APIs: Microsoft
identity sign-in, the ad accounts a user can reach (Customer Management) and daily reports
(Reporting: submit, poll, download a zipped CSV).

A consent buys a refresh token. Microsoft rotates it: every token request may return a new one,
and the old one stays valid, so `refresh_grant` returns the newest for the application to keep.
Each `read` and `list_accounts` trades the stored refresh token for an access token (about an
hour) and drops whatever new refresh token comes with it.

A report's download link is a pre-signed storage URL, which is a credential: it is fetched once
by a client that sends no other credential, and is never logged, kept or put in an error.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
import time
import zipfile
import zlib
from collections.abc import Mapping
from datetime import date
from typing import Any

import httpx

from data_sources.core.logging import logger
from data_sources.reports.base import Account, Grant, ReportSource
from data_sources.reports.errors import Problem, ReportError
from data_sources.reports.http import ApiClient, Retry, Sleep, Verdict
from data_sources.reports.models import RawReport, parse_day
from data_sources.reports.oauth import consent_url, email_from_id_token

AUTH_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
SCOPE = "https://ads.microsoft.com/msads.manage"
#: What the consent asks for (the OpenID scopes name who consented); token requests ask for less.
CONSENT_SCOPES = (SCOPE, "offline_access", "openid", "email", "profile")
TOKEN_SCOPES = (SCOPE, "offline_access")
HOSTS = {
    "production": {
        "reporting": "https://reporting.api.bingads.microsoft.com/Reporting/v13/",
        "clientcenter": "https://clientcenter.api.bingads.microsoft.com/CustomerManagement/v13/",
    },
    "sandbox": {
        "reporting": "https://reporting.api.sandbox.bingads.microsoft.com/Reporting/v13/",
        "clientcenter": (
            "https://clientcenter.api.sandbox.bingads.microsoft.com/CustomerManagement/v13/"
        ),
    },
}
ACCOUNT_PAGE_SIZE = 1000
MAX_ACCOUNT_PAGES = 100
#: The most a report's CSV may inflate to (and its zip weigh) before it is refused.
MAX_REPORT_BYTES = 512 * 1024 * 1024
#: Seconds between polls of a report being built: these, then the last one over and over.
POLL_BACKOFF = (2.0, 4.0, 8.0, 16.0, 30.0)
#: How long a report may take to be built, unless `options["report_timeout"]` says otherwise.
REPORT_TIMEOUT = 600.0
#: Fault codes worth retrying (internal error, call rate exceeded, ...), and those that mean the
#: token, the developer token or the user's access is the problem.
RETRY_CODES = {0, 117, 207, 833}
AUTH_CODES = {105, 106, 109, 1001}

UNREACHABLE = Problem("unavailable", "Microsoft Advertising could not be reached.")
BUSY = Problem("unavailable", "Microsoft Advertising is temporarily unavailable or rate limited.")
INVALID = Problem("unavailable", "Microsoft Advertising returned an invalid response.")
AUTH_FAILED = Problem(
    "auth_failed",
    "Microsoft Advertising refused the token, the developer token or the account access; "
    "authorize again.",
)
REFUSED = "Microsoft Advertising refused the request."
SIGN_IN_UNREACHABLE = Problem("unavailable", "Microsoft sign-in could not be reached.")
SIGN_IN_BUSY = Problem("unavailable", "Microsoft sign-in is unavailable; try again.")
SIGN_IN_REFUSED = "Microsoft refused the authorization code or the refresh token; authorize again."
EXPIRED_LINK = Problem("unavailable", "The report's download link expired; import again.")
TOO_LARGE = Problem("request_failed", "The Microsoft Advertising report is too large.")
DEFINITION = Problem("request_failed", "Unknown Microsoft Advertising report definition.")

_NAME = re.compile(r"[A-Za-z]+")
_REPORT = re.compile(r"[A-Za-z]+ReportRequest")
_TENANT = re.compile(r"[A-Za-z0-9.-]+")
_DIGITS = re.compile(r"[0-9]+")
_CHUNK = 1 << 20

_logger = logger.getChild("microsoft_ads.reports")

#: Download links being fetched, as httpx normalizes them (host lower-cased, default port dropped,
#: characters encoded): httpx logs every request's URL in that form, and these must not be logged.
_DOWNLOADS: set[str] = set()


def _normalized(value: httpx.URL | str) -> str | None:
    try:
        return str(httpx.URL(value))
    except (httpx.InvalidURL, TypeError, ValueError):
        return None


class _RedactDownloadLinks(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if _DOWNLOADS and isinstance(record.args, tuple):
            record.args = tuple(
                "[report download link]"
                if isinstance(value, httpx.URL | str) and _normalized(value) in _DOWNLOADS
                else value
                for value in record.args
            )
        return True


logging.getLogger("httpx").addFilter(_RedactDownloadLinks())


def _text(value: object, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def _code(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and _DIGITS.fullmatch(value):
        return int(value)
    return None


def _faults(response: httpx.Response) -> tuple[list[tuple[int | None, str, str]], str]:
    """`([(Code, ErrorCode, Message)], TrackingId)` of an `ApiFault` / `AdApiFaultDetail` body."""
    try:
        body = response.json()
    except ValueError:
        return [], ""
    if not isinstance(body, dict):
        return [], ""
    faults = [
        (_code(item.get("Code")), _text(item.get("ErrorCode")), _text(item.get("Message")))
        for key in ("OperationErrors", "BatchErrors", "Errors")
        if isinstance(body.get(key), list)
        for item in body[key]
        if isinstance(item, dict)
    ]
    return faults, _text(body.get("TrackingId"), 100)


def _diagnostics(faults: list[tuple[int | None, str, str]], tracking: str) -> str:
    said = "; ".join(
        ": ".join(
            part for part in (name or (str(code) if code is not None else ""), message) if part
        )
        for code, name, message in faults
    )
    return f"{said} (tracking {tracking})" if said and tracking else said


def classify(response: httpx.Response) -> Verdict:
    """Microsoft answers a fault as JSON (`OperationErrors`, `BatchErrors` or `Errors`, each
    `{Code, ErrorCode, Message}`, and a `TrackingId`) with an HTTP status that mostly agrees, so
    both are read."""
    faults, tracking = _faults(response)
    codes = {code for code, _, _ in faults if code is not None}
    if response.status_code == 429 or response.status_code >= 500 or codes & RETRY_CODES:
        return Retry(BUSY)
    if response.status_code == 401 or codes & AUTH_CODES:
        return Problem(AUTH_FAILED.reason, AUTH_FAILED.message, _diagnostics(faults, tracking))
    if response.is_error or faults:
        return Problem("request_failed", REFUSED, _diagnostics(faults, tracking))
    return None


def _token_classify(response: httpx.Response) -> Verdict:
    """The token endpoint answers OAuth's `{"error", "error_codes"}`: any refusal means the code or
    the refresh token is no good (or the app's client is misconfigured)."""
    if response.status_code == 429 or response.status_code >= 500:
        return Retry(SIGN_IN_BUSY)
    if response.is_error:
        try:
            body = response.json()
        except ValueError:
            body = None
        said = ""
        if isinstance(body, dict):
            codes = body.get("error_codes")
            aadsts = (
                ", ".join(f"AADSTS{c}" for c in codes if _code(c) is not None)
                if isinstance(codes, list)
                else ""
            )
            said = _text(body.get("error"), 60) + (f" ({aadsts[:100]})" if aadsts else "")
        return Problem("auth_failed", SIGN_IN_REFUSED, said)
    return None


def _download_classify(response: httpx.Response) -> Verdict:
    if response.status_code == 429 or response.status_code >= 500:
        return Retry(BUSY)
    if response.status_code in {403, 404}:
        return EXPIRED_LINK
    if response.is_error:
        return Problem("unavailable", "The Microsoft Advertising report could not be downloaded.")
    return None


def _id(value: object) -> str | None:
    """A Microsoft id (a long, sent as a number or a string) as digits."""
    code = _code(value)
    return str(code) if code is not None else None


def _account_ids(account: str) -> tuple[str, str]:
    """`"<account id>:<parent customer id>"`, both digits."""
    account_id, _, customer_id = account.partition(":")
    if not _DIGITS.fullmatch(account_id) or not _DIGITS.fullmatch(customer_id):
        raise Problem("request_failed", "Invalid Microsoft Advertising account.").error()
    return account_id, customer_id


def _spec(spec: Mapping[str, Any]) -> tuple[str, list[str], str]:
    """`(report type, columns, day column)` of a spec, checked before anything is sent: the type
    ends in `ReportRequest`, every column is a plain name, and there is at least one attribute and
    one metric."""
    report, day = spec.get("report"), spec.get("day_column")
    attributes, metrics = spec.get("attributes"), spec.get("metrics")
    names = [day, *(attributes or []), *(metrics or [])]
    if (
        not isinstance(report, str)
        or not _REPORT.fullmatch(report)
        or not isinstance(attributes, list | tuple)
        or not isinstance(metrics, list | tuple)
        or not attributes
        or not metrics
        or not all(isinstance(n, str) and _NAME.fullmatch(n) for n in names)
        or len(set(names)) != len(names)
    ):
        raise DEFINITION.error()
    return report, [str(n) for n in names], str(day)


def _date(day: date) -> dict[str, int]:
    return {"Day": day.day, "Month": day.month, "Year": day.year}


def _rows(content: bytes, columns: list[str]) -> list[dict[str, Any]]:
    """The rows of the one CSV in a report's zip, as `{column header: value}`. The CSV is read in
    chunks and refused once it inflates past `MAX_REPORT_BYTES`."""
    if len(content) > MAX_REPORT_BYTES:
        raise TOO_LARGE.error()
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            files = [info for info in archive.infolist() if not info.is_dir()]
            if len(files) != 1:
                raise INVALID.error()
            data, size = [], 0
            with archive.open(files[0]) as member:
                while chunk := member.read(_CHUNK):
                    size += len(chunk)
                    if size > MAX_REPORT_BYTES:
                        raise TOO_LARGE.error()
                    data.append(chunk)
        text = b"".join(data).decode("utf-8-sig")
    except (
        zipfile.BadZipFile,
        zlib.error,
        EOFError,
        NotImplementedError,  # a compression method zipfile can't read
        RuntimeError,  # an encrypted member
        UnicodeDecodeError,
    ):
        raise INVALID.error() from None
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None:
        return []  # nothing at all, not even the column headers
    if not set(columns) <= set(reader.fieldnames):
        raise INVALID.error()
    rows = []
    for row in reader:
        if None in row:  # more values than headers
            raise INVALID.error()
        rows.append(dict(row))
    return rows


class MicrosoftAdsReports(ReportSource):
    """Microsoft Advertising (`provider: microsoft_ads`): `client_id`, `client_secret`
    (an Entra ID web app), `developer_token` and `redirect_uri` in `auth.credentials`; `options`
    `environment` (`production` or `sandbox`), `tenant` (`common`) and `report_timeout` (seconds a
    report may take to be built, 600). The grant's credentials are `{"refresh_token", "scope"}`; an
    account is `"<account id>:<parent customer id>"`; a report spec is `{"report"` (a reporting
    `...ReportRequest` type), `"attributes", "metrics", "day_column"}`: the CSV's columns, read
    with daily aggregation. Records are the CSV's rows as `{column: text}`."""

    provider = "microsoft_ads"
    app_credentials = ("client_id", "client_secret", "developer_token")

    @property
    def tenant(self) -> str:
        tenant = self.option("tenant", "common")
        if not _TENANT.fullmatch(tenant):
            raise ReportError("oauth_not_configured", "Invalid Microsoft tenant.")
        return tenant

    @property
    def hosts(self) -> dict[str, str]:
        environment = self.option("environment", "production")
        if environment not in HOSTS:
            raise ReportError("oauth_not_configured", "Unknown Microsoft Advertising environment.")
        return HOSTS[environment]

    @property
    def report_timeout(self) -> float:
        try:
            value = float(self.option("report_timeout", str(REPORT_TIMEOUT)))
        except ValueError:
            return REPORT_TIMEOUT
        return value if value > 0 else REPORT_TIMEOUT

    def _client(self, token: str, account_id: str = "", customer_id: str = "") -> ApiClient:
        headers = {
            "Authorization": f"Bearer {token}",
            "DeveloperToken": self.credential("developer_token"),
        }
        if account_id:
            headers |= {"CustomerId": customer_id, "CustomerAccountId": account_id}
        return ApiClient(
            classify,
            unreachable=UNREACHABLE,
            invalid=INVALID,
            headers=headers,
            timeout=60,
            transport=self.transport,
            sleep=self.sleep,
        )

    # Sign-in

    def authorize_url(self, state: str) -> str:
        return consent_url(
            AUTH_URL.format(tenant=self.tenant),
            client_id=self.credential("client_id"),
            response_type="code",
            redirect_uri=self.credential("redirect_uri"),
            response_mode="query",
            scope=" ".join(CONSENT_SCOPES),
            prompt="select_account",
            state=state,
        )

    async def _token_request(self, params: dict[str, str], *, retry: bool) -> dict[str, Any]:
        if not self.credential("client_id") or not self.credential("client_secret"):
            raise ReportError("oauth_not_configured", "Configure the Microsoft Advertising app.")
        async with ApiClient(
            _token_classify,
            unreachable=SIGN_IN_UNREACHABLE,
            invalid=Problem("unavailable", "Microsoft sign-in returned an invalid response."),
            timeout=15,
            transport=self.transport,
            sleep=self.sleep,
        ) as api:
            return await api.json(
                "POST",
                TOKEN_URL.format(tenant=self.tenant),
                retry=retry,
                data={
                    "client_id": self.credential("client_id"),
                    "client_secret": self.credential("client_secret"),
                    "scope": " ".join(TOKEN_SCOPES),
                    **params,
                },
            )

    async def exchange_code(self, code: str) -> Grant:
        """`{"refresh_token", "scope"}`, and who consented as `identity` (the id_token's email,
        else its `preferred_username`). A consent that left Microsoft Advertising out is
        `access_denied`. Not retried: a code works once."""
        body = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.credential("redirect_uri"),
            },
            retry=False,
        )
        refresh = body.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            raise ReportError("no_refresh_token", "Microsoft returned no refresh token.")
        scope = body.get("scope")
        if "scope" in body and not _grants_ads(scope):
            raise ReportError("access_denied", "The consent didn't grant Microsoft Advertising.")
        return Grant(
            {"refresh_token": refresh, "scope": str(scope or "")},
            identity=email_from_id_token(body.get("id_token"), ("email", "preferred_username")),
        )

    async def _refresh(self, credentials: Mapping[str, Any]) -> dict[str, Any]:
        refresh = credentials.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            raise Problem("auth_failed", "Authorize this source again.").error()
        body = await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh}, retry=True
        )
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise Problem("auth_failed", SIGN_IN_REFUSED).error()
        return body

    async def refresh_grant(self, credentials: Mapping[str, Any]) -> Grant:
        """The grant with the newest refresh token Microsoft handed out (the stored one when none
        came back). `auth_failed` once the refresh token stopped working: authorize again."""
        body = await self._refresh(credentials)
        rotated = body.get("refresh_token")
        refresh = rotated if isinstance(rotated, str) and rotated else credentials["refresh_token"]
        scope = body.get("scope") or credentials.get("scope")
        return Grant({"refresh_token": refresh, "scope": str(scope or "")})

    async def _access_token(self, credentials: Mapping[str, Any]) -> str:
        return str((await self._refresh(credentials))["access_token"])

    # Accounts

    async def list_accounts(self, credentials: Mapping[str, Any]) -> list[Account]:
        """Every ad account the consenting user can reach, whatever its status (`details`:
        `account_id`, `customer_id` (the parent customer), `number`, `status`)."""
        base = self.hosts["clientcenter"]
        async with self._client(await self._access_token(credentials)) as api:
            body = await api.json("POST", f"{base}User/Query", retry=True, json={"UserId": None})
            user = body.get("User")
            user_id = _id(user.get("Id")) if isinstance(user, dict) else None
            if user_id is None:
                raise INVALID.error()
            accounts: list[Account] = []
            for index in range(MAX_ACCOUNT_PAGES):
                body = await api.json(
                    "POST",
                    f"{base}Accounts/Search",
                    retry=True,
                    json={
                        "Predicates": [{"Field": "UserId", "Operator": "Equals", "Value": user_id}],
                        "Ordering": None,
                        "PageInfo": {"Index": index, "Size": ACCOUNT_PAGE_SIZE},
                    },
                )
                page = body.get("Accounts")
                if not isinstance(page, list) or any(not isinstance(a, dict) for a in page):
                    raise INVALID.error()
                accounts.extend(_account(found) for found in page)
                if len(page) < ACCOUNT_PAGE_SIZE:
                    return accounts
        raise INVALID.error()  # pages that never end

    # Reports

    async def read(
        self,
        credentials: Mapping[str, Any],
        account: str,
        spec: Mapping[str, Any],
        since: date,
        until: date,
    ) -> RawReport:
        """The report `spec` describes for `account` over `since..until`, by day: submitted,
        polled until built, downloaded once. A report built with no download link is empty."""
        account_id, customer_id = _account_ids(account)
        report, columns, day_column = _spec(spec)
        base = self.hosts["reporting"]
        envelope = {
            "report": report,
            "columns": columns,
            "account_id": account_id,
            "customer_id": customer_id,
        }
        request = {
            "ReportRequest": {
                "Type": report,
                "Aggregation": "Daily",
                "Format": "Csv",
                "FormatVersion": "2.0",
                "ExcludeReportHeader": True,
                "ExcludeReportFooter": True,
                "ExcludeColumnHeaders": False,
                "ReturnOnlyCompleteData": False,
                "Columns": columns,
                "Scope": {"AccountIds": [account_id]},
                "Time": {"CustomDateRangeStart": _date(since), "CustomDateRangeEnd": _date(until)},
            }
        }
        budget = _Budget(self.report_timeout, self.sleep)
        try:
            # The wall-clock limit also covers slow requests and the download; the budget's own
            # check ends polling even when `sleep` doesn't really wait (tests).
            content = await asyncio.wait_for(
                self._fetch(credentials, base, request, account_id, customer_id, budget),
                budget.timeout,
            )
        except TimeoutError:
            raise budget.expired() from None
        if content is None:
            _logger.info(f"{report} for account {account_id} is empty")
            return RawReport([], [], envelope)
        records = _rows(content, columns)
        _logger.info(f"Read {len(records)} {report} rows for account {account_id}")
        return RawReport(
            records, [parse_day(row.get(day_column) or "") for row in records], envelope
        )

    async def _fetch(
        self,
        credentials: Mapping[str, Any],
        base: str,
        request: dict[str, Any],
        account_id: str,
        customer_id: str,
        budget: _Budget,
    ) -> bytes | None:
        """The built report's zip (None: built, and empty): token, submit, poll, download."""
        token = await self._access_token(credentials)
        async with self._client(token, account_id, customer_id) as api:
            submitted = await api.json(
                "POST", f"{base}GenerateReport/Submit", retry=True, json=request
            )
            request_id = submitted.get("ReportRequestId")
            if not isinstance(request_id, str) or not request_id:
                raise INVALID.error()
            link = await self._poll(api, base, request_id, budget)
        return None if link is None else await self._download(link)

    async def _poll(
        self, api: ApiClient, base: str, request_id: str, budget: _Budget
    ) -> str | None:
        """The report's download link once it is built (None: built, and empty), polled with
        `POLL_BACKOFF` until the budget is spent."""
        attempt = 0
        while True:
            body = await api.json(
                "POST",
                f"{base}GenerateReport/Poll",
                retry=True,
                json={"ReportRequestId": request_id},
            )
            status = body.get("ReportRequestStatus")
            if not isinstance(status, dict):
                raise INVALID.error()
            state = status.get("Status")
            if state == "Success":
                link = status.get("ReportDownloadUrl")
                if link is None or link == "":
                    return None
                if not isinstance(link, str) or not link.startswith("https://"):
                    raise INVALID.error()
                return link
            if state != "Pending":
                raise Problem(
                    "request_failed",
                    "Microsoft Advertising could not build the report.",
                    f"Status: {_text(state, 40)}",
                ).error()
            if budget.left <= 0:
                raise budget.expired()
            await budget.sleep(min(POLL_BACKOFF[min(attempt, len(POLL_BACKOFF) - 1)], budget.left))
            attempt += 1

    async def _download(self, link: str) -> bytes:
        """The zip behind `link`, through a client that sends no credential of its own, read
        as it arrives and refused past `MAX_REPORT_BYTES`."""
        logged = _normalized(link)
        if logged is None:
            raise INVALID.error()
        _DOWNLOADS.add(logged)
        try:
            async with ApiClient(
                _download_classify,
                unreachable=Problem(
                    "unavailable", "The Microsoft Advertising report could not be downloaded."
                ),
                invalid=INVALID,
                timeout=120,
                transport=self.transport,
                sleep=self.sleep,
            ) as api:
                response = await api.request(
                    "GET", link, max_bytes=MAX_REPORT_BYTES, too_large=TOO_LARGE
                )
                return response.content
        finally:
            _DOWNLOADS.discard(logged)


class _Budget:
    """How long one report may take, counted from the start of the read: the time spent in requests
    (monotonic clock) plus the time asked of `sleep`, so a sleep that doesn't really wait still
    uses the budget up and a real one isn't counted twice."""

    def __init__(self, timeout: float, sleep: Sleep) -> None:
        self.timeout = timeout
        self._sleep = sleep
        self._start = time.monotonic()
        self._slept = 0.0  # what `sleep` was asked for
        self._sleeping = 0.0  # the clock time those sleeps really took

    @property
    def left(self) -> float:
        working = time.monotonic() - self._start - self._sleeping
        return self.timeout - working - self._slept

    async def sleep(self, seconds: float) -> None:
        began = time.monotonic()
        await self._sleep(seconds)
        self._sleeping += time.monotonic() - began
        self._slept += seconds

    def expired(self) -> ReportError:
        return ReportError(
            "report_timeout",
            f"The Microsoft Advertising report wasn't ready after {self.timeout:g} seconds.",
        )


def _grants_ads(scope: object) -> bool:
    return isinstance(scope, str) and any(
        granted == SCOPE or granted.endswith("/msads.manage") for granted in scope.split()
    )


def _account(found: dict[str, Any]) -> Account:
    account_id, customer_id = _id(found.get("Id")), _id(found.get("ParentCustomerId"))
    if account_id is None or customer_id is None:
        raise INVALID.error()
    return Account(
        id=f"{account_id}:{customer_id}",
        name=_text(found.get("Name"), 200),
        currency=_text(found.get("CurrencyCode"), 10),
        timezone=_text(found.get("TimeZone"), 100),
        details={
            "account_id": account_id,
            "customer_id": customer_id,
            "number": _text(found.get("Number"), 50),
            "status": _text(found.get("AccountLifeCycleStatus"), 50),
        },
    )
