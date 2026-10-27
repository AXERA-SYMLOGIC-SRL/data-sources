"""Google Ads diagnostics for server logs, with request credentials removed."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any
from urllib.parse import parse_qs, quote, quote_plus

import httpx

from data_sources.core.logging import logger

_logger = logger.getChild("google_ads.client")


def google_ads_error_codes(response: httpx.Response) -> set[str]:
    """Read provider enums only, for narrowly classifying account-specific failures."""
    try:
        body = response.json()
    except ValueError:
        return set()
    error = body.get("error") if isinstance(body, dict) else None
    details = error.get("details") if isinstance(error, dict) else None
    codes: set[str] = set()
    for detail in details if isinstance(details, list) else []:
        errors = detail.get("errors") if isinstance(detail, dict) else None
        for entry in errors if isinstance(errors, list) else []:
            values = entry.get("errorCode") if isinstance(entry, dict) else None
            if isinstance(values, dict):
                codes.update(value for value in values.values() if isinstance(value, str))
    return codes


def log_google_ads_error(response: httpx.Response, secrets: Iterable[str] = ()) -> None:
    """Log what Google said about a refused request: status, endpoint, request id and Google's
    error codes and messages, with every credential of the request (and `secrets`, the app's
    client secret and developer token) removed. Bodies, headers and queries are never logged."""
    request = response.request
    secrets = set(secrets)
    bearer = request.headers.get("authorization", "")
    if bearer.lower().startswith("bearer "):
        secrets.add(bearer[7:])
    secrets.add(request.headers.get("developer-token", ""))
    if "application/x-www-form-urlencoded" in request.headers.get("content-type", ""):
        for key, values in parse_qs(request.content.decode("utf-8", errors="replace")).items():
            if key in {"client_secret", "refresh_token", "access_token", "code"}:
                secrets.update(values)
    variants: set[str] = set()
    for secret in secrets - {""}:
        variants.update(
            (secret, quote(secret, safe=""), quote_plus(secret), json.dumps(secret)[1:-1])
        )

    def clean(value: object, limit: int = 2000) -> str:
        text = str(value)
        for secret in sorted(variants, key=len, reverse=True):
            text = text.replace(secret, "[redacted]")
        # Keep a provider message on one log line and bound its size.
        return " ".join(text.split())[:limit]

    details: dict[str, Any] = {
        "http_status": response.status_code,
        "method": request.method,
        "endpoint": clean(f"{request.url.host}{request.url.path}"),
    }
    request_id = response.headers.get("request-id") or response.headers.get("x-request-id")
    if request_id:
        details["request_id"] = clean(request_id, 200)
    try:
        body = response.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, str):
        details["oauth_error"] = clean(error, 200)
        if isinstance(body.get("error_description"), str):
            details["message"] = clean(body["error_description"])
    elif isinstance(error, dict):
        for key in ("code", "status", "message"):
            if isinstance(error.get(key), (str, int)):
                details[key] = clean(error[key])
        extra = error.get("details")
        for item in extra[:10] if isinstance(extra, list) else []:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("requestId"), str):
                details["request_id"] = clean(item["requestId"], 200)
            if isinstance(item.get("reason"), str):
                details.setdefault("reasons", []).append(clean(item["reason"], 200))
            errors = item.get("errors")
            for entry in errors[:10] if isinstance(errors, list) else []:
                if not isinstance(entry, dict):
                    continue
                diagnostic: dict[str, Any] = {}
                codes = entry.get("errorCode")
                if isinstance(codes, dict):
                    diagnostic["error_code"] = {
                        clean(key, 100): clean(value, 200)
                        for key, value in list(codes.items())[:10]
                        if isinstance(value, (str, int))
                    }
                if isinstance(entry.get("message"), str):
                    diagnostic["message"] = clean(entry["message"])
                if diagnostic:
                    details.setdefault("errors", []).append(diagnostic)
    else:
        details["message"] = "Provider returned no structured error details."
    # Deliberately omit full bodies, headers, GAQL, error triggers and token payloads.
    diagnostic_json = json.dumps(details, ensure_ascii=False)
    while len(diagnostic_json) > 16000 and details.get("errors"):
        details["errors"].pop()
        details["truncated_errors"] = details.get("truncated_errors", 0) + 1
        diagnostic_json = json.dumps(details, ensure_ascii=False)
    _logger.warning(f"Google Ads provider error: {diagnostic_json}")
