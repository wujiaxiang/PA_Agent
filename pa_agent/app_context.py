"""Application context wiring shared resources without global singletons.

## ``ctx.settings`` 是「按请求解析」的，读写方式不变

配置已按用户分层（系统兜底 ← 用户覆盖），但把它接进请求路径时有个硬约束：
``ctx.settings`` 被**分析主流程、后台调度器、经验库结算**等大量**没有 request**
的代码读取。若改成「路由里各自解析」，那些非请求路径就断了；若废掉
``ctx.settings``，改动面会横跨半个仓库。

因此本模块把「请求内 / 请求外」的差别**收敛到一个 ContextVar**：

* :attr:`AppContext.settings` 读 → 请求内返回**本请求用户**解析出的配置；
  请求外（后台线程、调度器、启动）返回**启动时的默认解析**。
* 赋值 → 请求内只改本请求的副本（绝不污染全局默认，否则用户 A 读一次
  ``GET /api/settings`` 就会把全局配置换成他自己的）；请求外改全局默认。

**调用方一行都不用改**：135 处 ``ctx.settings`` 全部自动按用户取对值，
也不需要每个路由手写一遍解析。绑定动作由 ``web/server.py`` 的中间件统一完成。

ContextVar ��� :obj:`threading.local` 的区别在 asyncio 下是关键：每个请求
是一个独立的 Task，ContextVar 随 Task 复制，**天然不会串**。
"""
from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

#: 当前请求生效的配置。``None`` = 不在任何请求上下文中（后台线程/调度器/启动），
#: 此时 :attr:`AppContext.settings` 退回启动时的默认解析。
_request_settings: ContextVar[Any] = ContextVar("pa_agent_request_settings", default=None)


def bind_request_settings(settings: Any) -> Token:
    """把 *settings* 绑定为「当前请求生效配置」，返回用于还原的 Token。

    由 Web 中间件在每个请求入口调用一次。返回 Token 而非无参还原，
    是为了支持嵌套绑定（内层还原不会误伤外层）。
    """
    return _request_settings.set(settings)


def reset_request_settings(token: Token) -> None:
    """还原上一个绑定值。请求结束时必须调用，否则值会随 Task 泄漏。"""
    try:
        _request_settings.reset(token)
    except ValueError:  # pragma: no cover - 跨 Context 还原（如在线程里 reset）
        _request_settings.set(None)


def current_request_settings() -> Any:
    """当前请求生效的配置；不在请求上下文中时返回 ``None``。"""
    return _request_settings.get()


@dataclass(slots=True)
class AppContext:
    """Carries shared resources to GUI widgets and orchestrators."""

    #: 启动时的默认解析结果（无请求上下文时 ``settings`` 返回的就是它）。
    #: **不是** ``settings`` 字段本身 —— 那个名字让给了下面的 property，
    #: 否则 dataclass 生成的 ``__init__`` 会用字段覆盖掉 property。
    _default_settings: Any = None
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("pa_agent"))
    event_bus: Any = None

    # Data layer
    data_source: Any = None       # DataSource implementation

    # AI / orchestration layer
    client: Any = None            # DeepSeekClient
    assembler: Any = None         # PromptAssembler
    router: Any = None            # route_strategy_files callable
    validator: Any = None         # JsonValidator
    pending_writer: Any = None    # PendingWriter
    exp_reader: Any = None        # ExperienceReader
    ledger: Any = None            # SessionTokenLedger

    # Web route shared state
    _last_record: Any = None      # Last completed AnalysisRecord (for chat followup)

    @property
    def settings(self) -> Any:
        """生效配置。请求内按用户解析，请求外回落启动默认值。

        读端语义见模块 docstring。**不要**把它改回普通字段 —— 那是本次改造
        之前「所有用户共用一份配置」的根因。
        """
        scoped = _request_settings.get()
        return scoped if scoped is not None else self._default_settings

    @settings.setter
    def settings(self, value: Any) -> None:
        if _request_settings.get() is not None:
            # 请求内：只换本请求的副本。写全局默认会造成**跨用户串配置** ——
            # 用户 A 打开一次设置页，后台调度器就改用 A 的配置去跑结算。
            _request_settings.set(value)
        else:
            self._default_settings = value


    @classmethod
    def bootstrap(cls) -> "AppContext":
        """Wire all real components and return a fully initialised AppContext."""
        from pa_agent.config.paths import (
            SETTINGS_JSON_PATH,
            RECORDS_PENDING_DIR,
            EXPERIENCE_DIR,
            PROMPT_DIR,
        )
        from pa_agent.config.settings import load_settings
        from pa_agent.util.logging import configure_logging, update_api_key
        from pa_agent.util.event_bus import EventBus
        from pa_agent.util.mask_secret import mask_secret
        from pa_agent.data.factory import create_data_source, normalize_data_source_kind
        from pa_agent.ai.client_factory import create_ai_client
        from pa_agent.ai.prompt_assembler import PromptAssembler
        from pa_agent.ai.router import route_strategy_files
        from pa_agent.ai.json_validator import JsonValidator
        from pa_agent.ai.session_ledger import SessionTokenLedger
        from pa_agent.records.pending_writer import PendingWriter
        from pa_agent.records.experience_reader import ExperienceReader

        # ── Settings ──────────────────────────────────────────────────────────
        # 解析的是**启动时的默认用户**（``load_settings(user_id=None)``）。这是
        # 非请求上下文（后台调度器 / 经验库结算 / 启动）唯一的配置来源；
        # 请求路径由 Web 中间件按 ``current_user_id(request)`` 另行绑定。
        settings = load_settings(SETTINGS_JSON_PATH)
        from pa_agent.ai.qclaw_connector import sync_qclaw_agent_provider_on_load
        from pa_agent.ai.workbuddy_connector import sync_workbuddy_provider_on_load
        from pa_agent.ai.cursor_connector import sync_cursor_provider_on_load
        from pa_agent.ai.trae_connector import sync_trae_cn_provider_on_load
        from pa_agent.ai.qoder_connector import sync_qoder_cn_provider_on_load

        # 同步后**不再整份写文件**：系统兜底一旦存在，settings.json 根本没人读，
        # 写了也是静默丢弃。各 sync_* 内部由 apply_*_provider_to_settings 调
        # `persist_provider()` 声明式写用户层（只写 connector 真正改的那几个键）。
        sync_qclaw_agent_provider_on_load(settings)
        sync_workbuddy_provider_on_load(settings)
        sync_cursor_provider_on_load(settings)
        sync_trae_cn_provider_on_load(settings)
        sync_qoder_cn_provider_on_load(settings)

        # Apply .env / process env overrides (TODO P1.2). Web mode typically
        # uses .env for secrets; GUI mode leaves .env absent and this is no-op.
        from pa_agent.config.env_loader import apply_env_overrides

        apply_env_overrides(settings)

        # ── Logging (with API key masking) ────────────────────────────────────
        configure_logging(api_key=settings.provider.api_key)

        app_logger = logging.getLogger("pa_agent")

        # ── Event bus ─────────────────────────────────────────────────────────
        event_bus = EventBus()

        # ── Data layer ────────────────────────────────────────────────────────
        from pa_agent.data.kline_adjust import apply_kline_adjust_from_settings

        apply_kline_adjust_from_settings(settings)
        ds_kind = normalize_data_source_kind(
            getattr(settings.general, "last_data_source", "mt5")
        )
        data_source = create_data_source(ds_kind)

        # Subscribe to the last-used symbol/timeframe from settings
        try:
            data_source.connect()
            if ds_kind == "tradingview":
                from pa_agent.data.tradingview import TradingViewSource

                if isinstance(data_source, TradingViewSource):
                    # Use saved exchange setting, default to auto (empty).
                    saved_exchange = getattr(settings.general, 'last_tradingview_exchange', '') or ''
                    data_source.set_exchange(saved_exchange)
            data_source.subscribe(
                settings.general.last_symbol,
                settings.general.last_timeframe,
            )
            app_logger.info(
                "Data source %s subscribed to %s %s",
                ds_kind,
                settings.general.last_symbol,
                settings.general.last_timeframe,
            )
        except Exception as exc:  # noqa: BLE001
            app_logger.warning("Initial data source subscription failed: %s", exc)

        # ── AI client ─────────────────────────────────────────────────────────
        from pa_agent.ai.client_factory import create_ai_client

        client = create_ai_client(settings.provider, logger_=app_logger)

        # ── Prompt assembler ──────────────────────────────────────────────────
        exp_reader = ExperienceReader(experience_dir=EXPERIENCE_DIR, logger=app_logger)
        assembler = PromptAssembler(
            prompt_dir=PROMPT_DIR,
            experience_reader=exp_reader,
            prompt_settings=settings.prompt,
        )

        # ── Validator & router ────────────────────────────────────────────────
        validator = JsonValidator(settings)
        router = route_strategy_files

        # ── Pending writer ────────────────────────────────────────────────────
        pending_writer = PendingWriter(
            pending_dir=RECORDS_PENDING_DIR,
            event_bus=event_bus,
            api_key=settings.provider.api_key,
        )

        # ── Session ledger ────────────────────────────────────────────────────
        ledger = SessionTokenLedger(
            context_window=settings.provider.context_window,
            warn_pct=settings.general.context_warning_threshold_pct,
        )

        return cls(
            _default_settings=settings,
            logger=app_logger,
            event_bus=event_bus,
            data_source=data_source,
            client=client,
            assembler=assembler,
            router=router,
            validator=validator,
            pending_writer=pending_writer,
            exp_reader=exp_reader,
            ledger=ledger,
        )
