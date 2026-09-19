"""Secret hygiene / redaction.

Browser profiles, cookies, tokens, and any authentication material are
sensitive. Everything that crosses from the browser layer to the API, logs,
reports, model prompts, or export bundles passes through `redact()`.

Design:
- deny-by-default: any dict key that *looks* credential-shaped is dropped or
  masked unless explicitly allow-listed as safe metadata.
- URLs keep host/path but strip query strings that can embed session tokens
  (e.g. Amazon's `ref`, `session`, `sid` params are removed conservatively).
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Keys whose values must never leave the server. Matched case-insensitively.
# `auth`/`session`/`sid` are matched on separator boundaries (they over-match
# as plain substrings — e.g. 'author'); credential compound keys like
# 'session-token'/'sessionid' are caught explicitly. The research-domain key
# 'session_id' (a UUID, not a credential) deliberately survives so exports
# and API payloads keep their structure.
_SUBSTRING_KEYS = (
    "password", "passwd", "secret", "token", "authorization", "cookie",
    "csrf", "xsrf", "api_key", "apikey", "credential", "bearer",
    "set-cookie", "awsuser", "sessionid", "jsessionid", "phpsessid",
)

_SEGMENT_RE = re.compile(r"(?:^|[_\-:. ])(auth|sid|session)(?:$|[_\-:. ])", re.IGNORECASE)

# Keys explicitly allowed despite containing a sensitive word segment.
_ALLOWED_SEGMENT_KEYS = {"session_id", "session_ids", "session_dir"}


def _is_sensitive_key(key: str) -> bool:
    lowered = str(key).lower()
    if lowered in _ALLOWED_SEGMENT_KEYS:
        return False
    if _SEGMENT_RE.search(lowered):
        return True
    return any(part in lowered for part in _SUBSTRING_KEYS)

# Query parameters commonly carrying tracking/session material.
_STRIPPED_QUERY_PARAMS = {
    "session", "sid", "session-id", "sessionid", "token", "csrf", "xsrf",
    "asin_token", "sig",
}

_MASK = "***"


def scrub_url(url: str) -> str:
    """Remove session-bearing query parameters from a URL."""
    if not url:
        return url
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if not parsed.query:
        return url
    kept = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _STRIPPED_QUERY_PARAMS
    ]
    return urlunparse(parsed._replace(query=urlencode(kept)))


def redact(value: Any, *, keep_urls: bool = True) -> Any:
    """Recursively sanitize a JSON-ish structure for exposure."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if _is_sensitive_key(str(k)):
                # Never include the value at all.
                continue
            out[str(k)] = redact(v, keep_urls=keep_urls)
        return out
    if isinstance(value, list):
        return [redact(v, keep_urls=keep_urls) for v in value]
    if isinstance(value, tuple):
        return [redact(v, keep_urls=keep_urls) for v in value]
    if isinstance(value, str):
        s = value
        if keep_urls and (s.startswith("http://") or s.startswith("https://")):
            return scrub_url(s)
        # Mask strings that look like bearer tokens / long secrets. Threshold
        # is 40+ chars so 32-char UUID ids survive; secrets.token_urlsafe(32)
        # tokens are 43 chars and are masked.
        if re.fullmatch(r"[A-Za-z0-9_\-=]{40,}", s):
            return _MASK
        return s
    return value


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Headers are dangerous; only a safe allow-list survives."""
    allowed = {"content-type", "content-length", "user-agent", "accept-language", "referer"}
    return {k: v for k, v in headers.items() if k.lower() in allowed}


def redact_cookies_summary(count: int, domains: list[str]) -> dict[str, Any]:
    """The ONLY cookie information ever exposed: a count and cookie domains."""
    return {"cookie_count": count, "cookie_domains": sorted(set(domains))}


def assert_no_secrets(value: Any, path: str = "") -> None:
    """Guard: raise if sensitive material is found in an outgoing structure."""
    if isinstance(value, dict):
        for k, v in value.items():
            if _is_sensitive_key(str(k)):
                raise AssertionError(f"sensitive key {k!r} found at {path}")
            assert_no_secrets(v, f"{path}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            assert_no_secrets(v, f"{path}[{i}]")
