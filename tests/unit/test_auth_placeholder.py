"""认证占位层测试：令牌、口令、注册原语、请求身份解析。

覆盖将来做注册登录时最容易出错、也最难靠人工点出来的边界。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from pa_agent.storage import auth as auth_mod
from pa_agent.storage.auth import (
    AuthError, hash_password, issue_token, verify_password, verify_token,
)


@pytest.fixture(autouse=True)
def _fixed_secret(monkeypatch):
    """固定密钥，避免测试期间真的去 config/ 落盘一个随机 secret。"""
    monkeypatch.setenv("PA_AGENT_TOKEN_SECRET", "unit-test-secret")


# ── 令牌 ───────────────────────────────────────────────────────────────────────


def test_roundtrip_carries_subject():
    claims = verify_token(issue_token("alice", ttl_s=60))
    assert claims is not None and claims.user_id == "alice"


def test_tampered_payload_rejected():
    """改载荷必须失效 —— 否则任何人都能伪造身份。"""
    token = issue_token("alice", ttl_s=60)
    assert verify_token(token[:-2] + "AA") is None


def test_token_signed_with_other_key_rejected():
    assert verify_token(issue_token("alice"), secret=b"other") is None


def test_expired_token_rejected():
    """留 30s leeway 容忍时钟偏移，故用 -60s 验证真正过期的那一侧。"""
    assert verify_token(issue_token("alice", ttl_s=-60)) is None


def test_garbage_never_raises():
    """热路径上令牌无效是常态，不能让调用点到处 try/except。"""
    for junk in ("", "x", "a.b", "v1.x", "v2.a.b", "....", "v1.!!!.???"):
        assert verify_token(junk) is None


def test_payload_never_contains_credentials():
    """载荷只放身份与时效 —— 令牌常被记进日志。"""
    assert "api_key" not in issue_token("alice")


def test_empty_user_id_refused():
    with pytest.raises(AuthError):
        issue_token("")


def test_prefix_change_invalidates_old_tokens():
    """换算法时旧令牌按前缀识别并拒绝，不产生歧义。"""
    assert verify_token("v2." + issue_token("alice")[3:]) is None


# ── 口令 ───────────────────────────────────────────────────────────────────────


def test_password_roundtrip():
    h = hash_password("s3cret")
    assert verify_password("s3cret", h)
    assert not verify_password("wrong", h)


def test_password_hash_is_self_describing():
    """迭代数写进结果 —— 将来调高后旧口令仍可校验，不强制全员改密。"""
    algo, rounds, salt, dk = hash_password("x").split("$")
    assert algo == "pbkdf2_sha256" and rounds.isdigit()
    assert salt and dk


def test_salt_makes_identical_passwords_differ():
    """否则彩虹表直接命中，同密码的两个账号散列相同。"""
    assert hash_password("same") != hash_password("same")


def test_malformed_stored_hash_returns_false_not_raise():
    assert not verify_password("x", "garbage")
    assert not verify_password("x", "")


def test_unicode_password():
    assert verify_password("密码-abc", hash_password("密码-abc"))


# ── 注册 / 登录原语 ────────────────────────────────────────────────────────────


@pytest.fixture()
def users_db(tmp_path: Path):
    from pa_agent.storage.db import reset_hub_for_tests

    hub = reset_hub_for_tests(tmp_path / "auth.db")
    yield hub
    from pa_agent.storage.users import list_users  # noqa: F401


def test_create_user_is_not_admin_by_default(users_db):
    from pa_agent.storage.users import create_user, get_user

    create_user("bob", password="pw")
    row = get_user("bob")
    assert row["role"] == "user" and row["is_default"] == 0


def test_duplicate_user_rejected_without_overwriting_password(users_db):
    """重复建号必须报错，不能静默改掉别人的口令。"""
    from pa_agent.storage.users import authenticate, create_user

    create_user("carol", password="first")
    with pytest.raises(AuthError):
        create_user("carol", password="second")
    assert authenticate("carol", "first") == "carol"
    assert authenticate("carol", "second") is None


def test_authenticate_rejects_wrong_password(users_db):
    from pa_agent.storage.users import authenticate, create_user

    create_user("dave", password="right")
    assert authenticate("dave", "right") == "dave"
    assert authenticate("dave", "wrong") is None


def test_authenticate_unknown_user_looks_identical_to_wrong_password(users_db):
    """两者必须返回同样的 None —— 区分开就成了用户名枚举接口。"""
    from pa_agent.storage.users import authenticate, create_user

    create_user("erin", password="pw")
    assert authenticate("erin", "bad") is None
    assert authenticate("ghost", "bad") is None


def test_user_without_password_cannot_authenticate(users_db):
    """旧库迁移后 password_hash 是空串 —— 必须是未设口令而非空口令放行。"""
    from pa_agent.storage.users import authenticate, ensure_admin_user

    ensure_admin_user()
    assert authenticate("admin", "") is None
    assert authenticate("admin", "anything") is None


def test_user_id_charset_is_restricted(users_db):
    """user_id 会进 SQL 与令牌载荷，字符集必须收紧。"""
    from pa_agent.storage.users import create_user

    for bad in ("a/b", "a b", "a;DROP", "", "x" * 100):
        with pytest.raises(ValueError):
            create_user(bad, password="pw")


def test_set_password(users_db):
    from pa_agent.storage.users import authenticate, create_user, set_password

    create_user("frank", password="old")
    assert set_password("frank", "new") is True
    assert authenticate("frank", "old") is None
    assert authenticate("frank", "new") == "frank"
    assert set_password("ghost", "x") is False


# ── 请求身份解析 ───────────────────────────────────────────────────────────────


def _request(headers: dict) -> Request:
    scope = {
        "type": "http", "method": "GET", "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


def test_bearer_token_extracted():
    from web.api.auth_ctx import bearer_token

    assert bearer_token(_request({"Authorization": "Bearer abc.def.ghi"})) == "abc.def.ghi"


def test_bearer_scheme_case_insensitive():
    """RFC 7235：scheme 大小写不敏感。"""
    from web.api.auth_ctx import bearer_token

    assert bearer_token(_request({"Authorization": "bearer tok"})) == "tok"


def test_malformed_authorization_header_yields_nothing():
    from web.api.auth_ctx import bearer_token

    for h in ("abc", "Bearer", "Basic xyz", ""):
        assert bearer_token(_request({"Authorization": h})) == ""


def test_valid_token_resolves_to_its_subject():
    from web.api.auth_ctx import current_auth

    auth = current_auth(_request({"Authorization": f"Bearer {issue_token('alice')}"}))
    assert auth.user_id == "alice"
    assert auth.authenticated is True
    assert auth.source == "token"


def test_anonymous_falls_back_to_admin_but_is_marked_unauthenticated():
    """回落必须留痕 —— 否则鉴权接错了会很安静。"""
    from web.api.auth_ctx import current_auth
    from pa_agent.storage.users import ADMIN_USER_ID

    auth = current_auth(_request({}))
    assert auth.user_id == ADMIN_USER_ID
    assert auth.authenticated is False
    assert auth.source == "fallback"


def test_invalid_token_falls_back_rather_than_raising():
    """坏令牌不能变成 500 —— 未登录/过期是常态。"""
    from web.api.auth_ctx import current_auth

    auth = current_auth(_request({"Authorization": "Bearer garbage"}))
    assert auth.authenticated is False


def test_user_header_ignored_unless_explicitly_trusted(monkeypatch):
    """默认不信直传头：部署方没鉴权时，任何人都能冒充。"""
    import web.api.auth_ctx as ac

    monkeypatch.setattr(ac, "TRUST_USER_HEADER", False)
    assert current_auth_claim(_request({"X-User-Id": "mallory"})) is None

    monkeypatch.setattr(ac, "TRUST_USER_HEADER", True)
    assert ac.current_auth(_request({"X-User-Id": "mallory"})).user_id == "mallory"


def current_auth_claim(req):
    import web.api.auth_ctx as ac

    auth = ac.current_auth(req)
    return auth.user_id if auth.authenticated else None


def test_require_auth_raises_401_when_anonymous():
    """将来「必须登录」的接口用它；现在全局匿名回落，所以默认走不到。"""
    from fastapi import HTTPException

    from web.api.auth_ctx import require_auth

    with pytest.raises(HTTPException) as ei:
        require_auth(_request({}))
    assert ei.value.status_code == 401
    assert ei.value.headers.get("WWW-Authenticate") == "Bearer"


def test_require_auth_passes_with_valid_token():
    from web.api.auth_ctx import require_auth

    token = issue_token("alice")
    auth = require_auth(_request({"Authorization": f"Bearer {token}"}))
    assert auth.user_id == "alice" and auth.authenticated

def test_token_secret_env_wins(monkeypatch, tmp_path):
    """生产必须能用环境变量注入密钥，否则多实例间令牌互不认。"""
    monkeypatch.setenv("PA_AGENT_TOKEN_SECRET", "from-env")
    assert auth_mod.get_token_secret() == b"from-env"


def test_dev_secret_persisted_so_restart_keeps_tokens(monkeypatch, tmp_path):
    """未设环境变量时落盘：否则每次重启旧令牌全部失效。"""
    import pa_agent.config.paths as paths_mod

    monkeypatch.delenv("PA_AGENT_TOKEN_SECRET", raising=False)
    monkeypatch.setattr(paths_mod, "CONFIG_DIR", tmp_path)
    first = auth_mod.get_token_secret()
    assert (tmp_path / "token_secret").exists()
    assert auth_mod.get_token_secret() == first