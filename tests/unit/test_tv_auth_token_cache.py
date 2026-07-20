"""Unit tests for the tvDatafeed auth-token cache.

The cache lives in ``pa_agent.data.tradingview`` and is exercised by the
monkey-patched ``_patched_auth`` (assigned to ``TvDatafeed._TvDatafeed__auth``
by ``_patch_tvdatafeed_auth``). We mock ``requests.Session`` so no real
network call is made — these tests verify cache lookup / write / eviction
logic only.

Coverage:
- Cache hit (no network) returns cached token.
- Cache miss → network login → write-through to in-memory + disk.
- Expired cache entry is evicted and re-fetched.
- Failed login (no token in response) is NOT cached.
- Empty credentials short-circuit to None.
- Disk cache round-trips through ``_load_disk_cache`` / ``_save_disk_cache``.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pa_agent.data import tradingview as tv_mod
from pa_agent.data.tradingview import (
    _TV_TOKEN_TTL_S,
    _hash_password,
    _load_disk_cache,
    _save_disk_cache,
)
from tvDatafeed import TvDatafeed  # type: ignore[import]


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def fresh_cache(tmp_path, monkeypatch):
    """Point the cache file at a tmp dir and reset in-memory state.

    Also re-applies the auth monkey-patch so tests see the patched
    ``__auth`` even if they run before ``connect()`` is ever called.
    """
    cache_file = tmp_path / ".tv_token_cache.json"
    monkeypatch.setattr(tv_mod, "_TV_TOKEN_CACHE_FILE", cache_file)
    tv_mod._tv_token_cache.clear()
    # Ensure the monkey-patch is applied (idempotent)
    tv_mod._patch_tvdatafeed_auth()
    yield cache_file
    tv_mod._tv_token_cache.clear()


def _make_login_response(token: str | None, error: str | None = None) -> MagicMock:
    """Build a fake response object mimicking ``requests.post().json()``."""
    resp = MagicMock()
    payload: dict = {}
    if token:
        payload["user"] = {"auth_token": token}
    if error:
        payload["error"] = error
        payload["code"] = "rate_limit"
    resp.json.return_value = payload
    return resp


def _mock_session(resp: MagicMock) -> MagicMock:
    """Build a fake requests.Session whose .get / .post return ``resp``."""
    session = MagicMock()
    session.get.return_value = MagicMock()
    session.post.return_value = resp
    return session


def _invoke_auth(username: str, password: str) -> str | None:
    """Call the patched __auth method on a bare TvDatafeed instance."""
    tv = TvDatafeed.__new__(TvDatafeed)
    return TvDatafeed._TvDatafeed__auth(tv, username, password)


# ── tests ───────────────────────────────────────────────────────────────────


def test_hash_password_is_sha256_hex() -> None:
    """Password is hashed with SHA-256 and returned as 64-char hex."""
    h = _hash_password("hunter2")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)
    # Deterministic
    assert _hash_password("hunter2") == h
    # Different input → different hash
    assert _hash_password("hunter3") != h


def test_disk_round_trip(fresh_cache: Path) -> None:
    """_save_disk_cache writes JSON; _load_disk_cache reads it back."""
    tv_mod._tv_token_cache[("alice", _hash_password("pw1"))] = ("TOKEN_A", 1000.0)
    tv_mod._tv_token_cache[("bob", _hash_password("pw2"))] = ("TOKEN_B", 2000.0)
    _save_disk_cache()

    # File exists and is valid JSON
    assert fresh_cache.exists()
    raw = json.loads(fresh_cache.read_text(encoding="utf-8"))
    assert len(raw["tokens"]) == 2

    # Clear memory and reload
    tv_mod._tv_token_cache.clear()
    loaded = _load_disk_cache()
    assert len(loaded) == 2
    assert loaded[("alice", _hash_password("pw1"))] == ("TOKEN_A", 1000.0)
    assert loaded[("bob", _hash_password("pw2"))] == ("TOKEN_B", 2000.0)


def test_load_disk_cache_missing_file_returns_empty(tmp_path, monkeypatch) -> None:
    """A non-existent cache file yields an empty dict, no exception."""
    monkeypatch.setattr(tv_mod, "_TV_TOKEN_CACHE_FILE", tmp_path / "missing.json")
    tv_mod._tv_token_cache.clear()
    assert _load_disk_cache() == {}


def test_patched_auth_cache_hit_skips_network(fresh_cache: Path) -> None:
    """A fresh cached token is returned without hitting the network."""
    # Seed cache with a fresh token
    tv_mod._tv_token_cache[("u", _hash_password("p"))] = (
        "CACHED_JWT",
        time.time(),  # saved now → not expired
    )

    # Make requests.Session raise if called — cache hit must not reach it
    with patch("requests.Session", side_effect=AssertionError("network call unexpected")):
        result = _invoke_auth("u", "p")

    assert result == "CACHED_JWT"


def test_patched_auth_cache_miss_writes_cache(fresh_cache: Path) -> None:
    """Cache miss → network login → token written to in-memory + disk."""
    fake_resp = _make_login_response(token="FRESH_JWT_123")
    fake_session = _mock_session(fake_resp)

    with patch("requests.Session", return_value=fake_session):
        token = _invoke_auth("u", "p")

    assert token == "FRESH_JWT_123"
    # In-memory cache populated
    cache_key = ("u", _hash_password("p"))
    assert cache_key in tv_mod._tv_token_cache
    assert tv_mod._tv_token_cache[cache_key][0] == "FRESH_JWT_123"
    # Disk cache populated
    assert fresh_cache.exists()
    raw = json.loads(fresh_cache.read_text(encoding="utf-8"))
    assert any(e["token"] == "FRESH_JWT_123" for e in raw["tokens"])


def test_patched_auth_expired_entry_evicted(fresh_cache: Path) -> None:
    """An expired cache entry is evicted and re-fetched from network."""
    # Seed an expired token (saved_at well in the past)
    expired_at = time.time() - (_TV_TOKEN_TTL_S + 1)
    tv_mod._tv_token_cache[("u", _hash_password("p"))] = ("OLD_TOKEN", expired_at)

    fake_resp = _make_login_response(token="NEW_TOKEN")
    fake_session = _mock_session(fake_resp)

    with patch("requests.Session", return_value=fake_session):
        token = _invoke_auth("u", "p")

    assert token == "NEW_TOKEN"
    cache_key = ("u", _hash_password("p"))
    assert tv_mod._tv_token_cache[cache_key][0] == "NEW_TOKEN"
    # Old token evicted (replaced with new one)
    assert all(t != "OLD_TOKEN" for t, _ in tv_mod._tv_token_cache.values())


def test_failed_login_not_cached(fresh_cache: Path) -> None:
    """When login response has no token, nothing is cached."""
    fake_resp = _make_login_response(token=None, error="recaptcha_required")
    fake_session = _mock_session(fake_resp)

    with patch("requests.Session", return_value=fake_session):
        token = _invoke_auth("u", "p")

    assert token is None
    assert len(tv_mod._tv_token_cache) == 0
    assert not fresh_cache.exists()


def test_empty_credentials_returns_none(fresh_cache: Path) -> None:
    """Empty username/password short-circuits to None without touching cache."""
    with patch("requests.Session", side_effect=AssertionError("network unexpected")):
        assert _invoke_auth("", "p") is None
        assert _invoke_auth("u", "") is None
        assert _invoke_auth("", "") is None

    assert len(tv_mod._tv_token_cache) == 0


def test_disk_cache_survives_module_reload(fresh_cache: Path) -> None:
    """A token written in one process is loaded back on the next ``_patch_*`` call."""
    tv_mod._tv_token_cache[("persist_user", _hash_password("pw"))] = (
        "PERSIST_JWT",
        time.time(),
    )
    _save_disk_cache()

    # Simulate process restart: clear memory + re-invoke the patch loader
    tv_mod._tv_token_cache.clear()
    assert len(tv_mod._tv_token_cache) == 0

    # Reload from disk (mimics what _patch_tvdatafeed_auth does at startup)
    tv_mod._tv_token_cache.update(_load_disk_cache())
    assert len(tv_mod._tv_token_cache) == 1
    key = ("persist_user", _hash_password("pw"))
    assert key in tv_mod._tv_token_cache
    assert tv_mod._tv_token_cache[key][0] == "PERSIST_JWT"
