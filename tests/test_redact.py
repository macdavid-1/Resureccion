"""Tests for the redaction / secret-hygiene module."""
from __future__ import annotations

import pytest

from app.redact import assert_no_secrets, redact, redact_cookies_summary, redact_headers, scrub_url


def test_sensitive_keys_dropped() -> None:
    data = {
        "username": "owner",
        "password": "hunter2",
        "Cookies": "x=y; z",
        "nested": {"api_key": "123", "safe": "value"},
    }
    out = redact(data)
    assert "password" not in out
    assert "Cookies" not in out
    assert "api_key" not in out["nested"]
    assert out["username"] == "owner"
    assert out["nested"]["safe"] == "value"


def test_session_id_is_structural_not_secret() -> None:
    """Research session ids are UUIDs, not credentials — they must survive.

    Real credential-shaped session keys are still dropped.
    """
    out = redact({"session_id": "abc123", "session-token": "t", "sessionid": "s", "sid": "x"})
    assert out["session_id"] == "abc123"
    assert "session-token" not in out
    assert "sessionid" not in out
    assert "sid" not in out


def test_auth_word_does_not_over_match() -> None:
    out = redact({"author": "nobody", "authenticated": True, "auth": "secret"})
    assert out["author"] == "nobody"
    assert out["authenticated"] is True
    assert "auth" not in out


def test_url_scrubbing() -> None:
    url = "https://www.amazon.com/s?k=journals&session=SECRET123&ref=sr_pg_2"
    scrubbed = scrub_url(url)
    assert "SECRET123" not in scrubbed
    assert "k=journals" in scrubbed


def test_redact_scrubs_urls_in_data() -> None:
    out = redact({"url": "https://www.amazon.com/dp/B0X?sid=abc&tag=x"})
    assert "sid=abc" not in out["url"]


def test_long_hex_masked() -> None:
    token = "a" * 40
    assert redact({"value": token})["value"] == "***"


def test_header_allowlist() -> None:
    headers = {
        "Content-Type": "application/json",
        "Cookie": "session=abc",
        "Authorization": "Bearer xyz",
        "User-Agent": "Mozilla/5.0",
    }
    safe = redact_headers(headers)
    assert set(safe) == {"Content-Type", "User-Agent"}


def test_cookie_summary_only_counts() -> None:
    summary = redact_cookies_summary(3, ["amazon.com", ".amazon.de", "amazon.com"])
    assert summary == {"cookie_count": 3, "cookie_domains": [".amazon.de", "amazon.com"]}


def test_assert_no_secrets_raises() -> None:
    assert_no_secrets({"page": 1, "items": [1, 2]})
    with pytest.raises(AssertionError):
        assert_no_secrets({"items": [{"cookie:session": "x"}]})


def test_lists_tuples_and_scalars() -> None:
    assert redact([1, "two"]) == [1, "two"]
    assert redact((1, 2)) == [1, 2]
    assert redact(42) == 42
    assert redact(None) is None
