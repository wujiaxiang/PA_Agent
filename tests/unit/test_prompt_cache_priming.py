"""服务端 prompt cache 预热。

服务端对**逐字相同的前缀**做 prompt cache。实测（2026-10）：
同一 prompt 连发两次缓存率 100%；两次真实分析之间（间隔数小时）只有 0.2%
—— 缓存存活期远短于人工分析间隔，靠「等上一轮缓存」不现实。

因此真实请求前先发一条同前缀、``max_tokens=1`` 的廉价请求把前缀写进缓存。
本测试锁定「什么时候该预热、什么时候不该」的选择逻辑。
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from pa_agent.ai.deepseek_client import (
    DeepSeekClient,
    _primeable_prefix,
    _should_prime,
)


@dataclass
class _Cfg:
    # model 也必须在：预热请求要读 settings.model 来指定模型，
    # 缺失会在真正发请求前就 AttributeError（测试夹具踩过一次）
    model: str = "test-model"
    prompt_cache_prime: bool = True


class _Exploding:
    """任何调用都抛异常 —— 预热必须完全不冒泡。

    Mirrors the OpenAI client's nesting
    (``client.chat.completions.create``) so an AttributeError cannot be
    mistaken for a caught network failure.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError("network down")


# ── 选哪几段做前缀 ────────────────────────────────────────────────────────

def test_prefix_requires_system_message():
    assert _primeable_prefix([]) is None
    assert _primeable_prefix([{"role": "user", "content": "x" * 5000}]) is None


def test_prefix_skips_tiny_system_prompt():
    """太小的 system 预热不值得多跑一次网络往返。"""
    msgs = [{"role": "system", "content": "short"}, {"role": "user", "content": "x" * 50000}]
    assert _primeable_prefix(msgs) is None


def test_prefix_includes_first_large_user_message():
    """首个 user 消息（方法论，最大且稳定）应一并预热。"""
    msgs = [
        {"role": "system", "content": "S" * 3000},
        {"role": "user", "content": "U" * 40000},
        {"role": "assistant", "content": "previous"},
        {"role": "user", "content": "new bars"},
    ]
    got = _primeable_prefix(msgs)
    assert got is not None
    assert [m["role"] for m in got] == ["system", "user"]
    assert got[1]["content"] == "U" * 40000


def test_prefix_stops_before_small_user_message():
    msgs = [
        {"role": "system", "content": "S" * 3000},
        {"role": "user", "content": "tiny"},
    ]
    got = _primeable_prefix(msgs)
    assert got is not None
    assert [m["role"] for m in got] == ["system"]


def test_prefix_does_not_mutate_input():
    msgs = [{"role": "system", "content": "S" * 3000}]
    before = [dict(m) for m in msgs]
    _primeable_prefix(msgs)
    assert msgs == before


# ── 阈值 ────────────────────────────────────────────────────────────────

def test_should_prime_respects_switch():
    assert _should_prime(_Cfg(prompt_cache_prime=True), 50000) is True
    assert _should_prime(_Cfg(prompt_cache_prime=False), 50000) is False


def test_should_prime_requires_size_floor():
    """小 prompt 预热收益不抵一次往返。"""
    assert _should_prime(_Cfg(prompt_cache_prime=True), 100) is False
    assert _should_prime(_Cfg(prompt_cache_prime=True), 20000) is True


# ── 失败必须被吞掉 ───────────────────────────────────────────────────────

def test_priming_failure_never_raises():
    client = DeepSeekClient.__new__(DeepSeekClient)
    client._settings = _Cfg(prompt_cache_prime=True)
    client._client = _Exploding()
    # 不抛异常即通过；预热失败绝不能阻断真实请求
    client._maybe_prime_cache([
        {"role": "system", "content": "S" * 12000},
        {"role": "user", "content": "U" * 12000},
    ])
    assert client._client.calls, "应当尝试过预热"
    # 预热请求必须是廉价探针，不能预留完整 completion 预算
    assert client._client.calls[0]["max_tokens"] == 1
    assert client._client.calls[0]["stream"] is False


def test_priming_skipped_when_prefix_too_small():
    client = DeepSeekClient.__new__(DeepSeekClient)
    client._settings = _Cfg(prompt_cache_prime=True)
    client._client = _Exploding()
    client._maybe_prime_cache([{"role": "user", "content": "hi"}])
    assert client._client.calls == []
