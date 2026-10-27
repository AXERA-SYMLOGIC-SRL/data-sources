"""The HTTP client every report client talks to its provider through.

What differs between providers is how a response is read: Google Ads names a disabled customer
in a 403, Search Console reports quota in a 403, TikTok answers 200 with an error code in the
body. A client says that in its `classify` function; the retries, backoff, JSON checks and the
rule that no credential or raw response ends up in an error are shared.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

import httpx

from data_sources.reports.errors import Problem


@dataclass(frozen=True)
class Retry:
    """A transient answer (rate limit, 5xx): try again, and raise `problem` once the attempts
    run out. `after` is how long the provider asked to wait, when it said."""

    problem: Problem
    after: float | None = None


#: What `classify` makes of a response: None to accept it, `Retry` to try again, a `Problem`
#: to fail.
Verdict = Problem | Retry | None
Classify = Callable[[httpx.Response], Verdict]
Sleep = Callable[[float], Awaitable[Any]]

IDEMPOTENT = frozenset({"GET", "HEAD", "OPTIONS"})


def exponential(base: float = 0.5) -> Callable[[int], float]:
    """0.5 s, 1 s, 2 s, ... after the first, second, third failure."""
    return lambda attempt: base * 2**attempt


DEFAULT_BACKOFF = exponential()


def linear(step: float = 1.0) -> Callable[[int], float]:
    """1 s, 2 s, 3 s, ..."""
    return lambda attempt: step * (attempt + 1)


class ApiClient:
    """An `httpx.AsyncClient` with a provider's error rules. Use it as an async context
    manager. `transport` and `sleep` are for tests: a mock transport, a sleep that doesn't
    wait."""

    def __init__(
        self,
        classify: Classify,
        *,
        unreachable: Problem,
        invalid: Problem,
        base_url: str = "",
        headers: dict[str, str] | None = None,
        timeout: float = 30,
        attempts: int = 3,
        backoff: Callable[[int], float] = DEFAULT_BACKOFF,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.classify = classify
        self.unreachable = unreachable
        self.invalid = invalid
        self.attempts = attempts
        self.backoff = backoff
        self.sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=timeout, transport=transport
        )

    async def __aenter__(self) -> Self:
        await self._http.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._http.__aexit__(exc_type, exc, tb)

    async def request(
        self, method: str, url: str, *, retry: bool | None = None, **kwargs: Any
    ) -> httpx.Response:
        """The response once `classify` accepts it. Only idempotent methods are retried unless
        `retry` says so (a report query sent as POST is safe to repeat; creating something is
        not)."""
        repeat = method.upper() in IDEMPOTENT if retry is None else retry
        attempts = self.attempts if repeat else 1
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                response = await self._http.request(method, url, **kwargs)
            except httpx.HTTPError:
                # The exception's text can hold the URL, and with it a token in the query.
                if last:
                    raise self.unreachable.error() from None
                await self.sleep(self.backoff(attempt))
                continue
            verdict = self.classify(response)
            if isinstance(verdict, Retry):
                if last:
                    raise verdict.problem.error()
                wait = verdict.after if verdict.after is not None else self.backoff(attempt)
                await self.sleep(wait)
                continue
            if verdict is not None:
                raise verdict.error()
            return response
        raise AssertionError("unreachable")

    async def json(
        self, method: str, url: str, *, retry: bool | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        """The response's JSON object; anything else is `invalid`."""
        response = await self.request(method, url, retry=retry, **kwargs)
        return parse_object(response, self.invalid)


def parse_object(response: httpx.Response, invalid: Problem) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        raise invalid.error() from None
    if not isinstance(body, dict):
        raise invalid.error()
    return body


def http_status(
    *,
    unavailable: Problem,
    auth_failed: Problem,
    failed: Problem,
    auth_statuses: frozenset[int] = frozenset({401, 403}),
) -> Classify:
    """The plain rules: 429 and 5xx are retried, 401/403 mean the grant is bad, any other
    error fails."""

    def classify(response: httpx.Response) -> Verdict:
        if response.status_code == 429 or response.status_code >= 500:
            return Retry(unavailable)
        if response.status_code in auth_statuses:
            return auth_failed
        if response.is_error:
            return failed
        return None

    return classify
