"""Regression tests for credential scrubbing across logs and records.

Background: ``requests`` embeds the full URL in ``ConnectionError`` /
``TooManyRedirects`` strings, so a failed Feishu webhook POST wrote
``.../bot/v2/hook/<TOKEN>`` straight into ``logs/pa_agent.log``. The log
formatter only masked the AI provider key.
"""
from __future__ import annotations

import logging

import pytest

from pa_agent.util.mask_secret import (
    mask_secret,
    register_secret,
    register_secrets,
    registered_secrets,
    scrub,
    unregister_secret,
)
from pa_agent.util.logging import JsonlFormatter, MaskingFormatter, register_settings_secrets


@pytest.fixture(autouse=True)
def _clean_registry():
    before = set(registered_secrets())
    yield
    for s in set(registered_secrets()) - before:
        unregister_secret(s)


def test_mask_secret_keeps_last_four():
    assert mask_secret("abcdefghij").endswith("hij")
    assert mask_secret("abcd") == "abcd"
    assert mask_secret("") == ""


def test_short_values_are_not_registered():
    """Placeholders like '****' must not blank out unrelated log text."""
    register_secret("****")
    register_secret("abc")
    register_secret("")
    assert "****" not in registered_secrets()
    assert "abc" not in registered_secrets()


def test_scrub_redacts_registered_secret():
    register_secret("feishu-token-abcdef123456")
    out = scrub("POST failed for /hook/feishu-token-abcdef123456")
    assert "feishu-token-abcdef123456" not in out
    assert out.count("*") > 0


def test_scrub_handles_multiple_and_empty():
    assert scrub("no secrets here") == "no secrets here"
    assert scrub("") == ""
    assert scrub(None) is None


def test_masking_formatter_scrubs_registered_secret(caplog):
    register_secret("webhook-SECRETVALUE-9999")
    fmt = MaskingFormatter("%(message)s")
    record = logging.LogRecord(
        "t", logging.WARNING, __file__, 1,
        "飞书通知 HTTP 请求失败: url=/bot/v2/hook/webhook-SECRETVALUE-9999",
        None, None,
    )
    out = fmt.format(record)
    assert "webhook-SECRETVALUE-9999" not in out


def test_jsonl_formatter_scrubs_exception_text():
    """exc_info text is the actual leak vector; it must be scrubbed too."""
    register_secret("webhook-SECRETVALUE-9999")
    fmt = JsonlFormatter()
    try:
        raise RuntimeError("Max retries exceeded with url: /bot/v2/hook/webhook-SECRETVALUE-9999")
    except RuntimeError:
        import sys

        record = logging.LogRecord(
            "t", logging.WARNING, __file__, 1, "notify failed", None, sys.exc_info()
        )
    out = fmt.format(record)
    assert "webhook-SECRETVALUE-9999" not in out


def test_register_settings_secrets_covers_every_credential():
    from pa_agent.config.settings import Settings

    s = Settings()
    s.provider.api_key = "sk-provider-key-1234567890"
    s.feishu.webhook_url = "https://open.feishu.cn/open-apis/bot/v2/hook/FEISHU-TOKEN-1234567890"
    s.feishu.secret = "feishu-secret-abcdef"
    s.feishu.app_secret = "feishu-appsecret-abcdef"
    s.pushplus.token = "pushplus-token-abcdef1234"
    s.tushare.token = "tushare-token-abcdef1234"
    s.tradingview.session_id = "tv-session-abcdef1234"
    s.tradingview.password = "tv-password-abcdef1234"

    register_settings_secrets(s)
    secrets = registered_secrets()
    for value in (
        s.provider.api_key,
        s.feishu.webhook_url,
        s.feishu.secret,
        s.feishu.app_secret,
        s.pushplus.token,
        s.tushare.token,
        s.tradingview.session_id,
        s.tradingview.password,
    ):
        assert value in secrets, f"{value!r} not registered"


def test_pending_writer_sanitizes_rotated_key(tmp_path):
    """A rotated key must not be written in plaintext just because the
    writer still holds the old one."""
    from pa_agent.records.pending_writer import PendingWriter

    old_key = "sk-old-key-0123456789abcdef"
    new_key = "sk-new-key-9876543210abcdef"
    register_secret(new_key)  # rotation happened via update_api_key()

    writer = PendingWriter(pending_dir=tmp_path, event_bus=None, api_key=old_key)
    data = {"stage1": f"key={old_key}", "stage2": f"rotated={new_key}"}
    out = writer._sanitize(data, old_key)

    assert old_key not in str(out)
    assert new_key not in str(out), "rotated key must be scrubbed too"
    assert "*" in str(out)


def test_feishu_webhook_token_extracted_from_url():
    from pa_agent.notify.feishu_notifier import _webhook_token

    url = "https://open.feishu.cn/open-apis/bot/v2/hook/ABCDEF123456"
    assert _webhook_token(url) == "ABCDEF123456"
    assert _webhook_token("") == ""


def test_requests_connection_error_no_longer_leaks(tmp_path, monkeypatch):
    """End-to-end: the exact leak vector is now scrubbed before logging."""
    import requests

    from pa_agent.notify.feishu_notifier import _webhook_token

    token = "LEAKTOKEN-abcdef123456"
    register_secret(token)

    with pytest.raises(Exception) as excinfo:
        try:
            requests.post(
                f"https://nonexistent-host-xyz.invalid/open-apis/bot/v2/hook/{token}",
                timeout=2,
            )
        except Exception as exc:  # noqa: BLE001
            raise

    assert token in str(excinfo.value), "requests really does embed the URL"
    assert token not in scrub(str(excinfo.value))