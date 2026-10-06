"""Pydantic settings models for PA Agent."""
from __future__ import annotations
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DecisionStance = Literal["conservative", "balanced", "aggressive", "extreme_aggressive"]
DataSourceKind = Literal["mt5", "tradingview", "akshare", "eastmoney", "eastmoney_futures", "tushare"]
NormalizationMode = Literal["strict", "lenient"]


class AIProviderSettings(BaseModel):
    """AI provider connection and behaviour settings."""
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    # **用户是否自带模型配置**。False = 用系统出厂默认（前端不显示配置项，
    # 后端整段取 baseline，完全忽略本用户对 provider 的任何覆盖）。
    #
    # 为什么不用「稀疏覆盖」隐式表达：那样用户既看不出哪些字段是继承来的，也
    # 无法一键切回（得逐个清掉散落的覆盖键）。更隐蔽的是**遮蔽**——用户一旦覆盖过
    # 某个字段，系统此后对该字段的更新就永远到不了他这里，而界面上毫无提示。
    #
    # 放在 provider 段内，但它与其他字段语义相反：**无论此开关取何值，它都由用户
    # 自己决定**，不受「关闭时整段取 baseline」影响。
    use_custom: bool = False

    # 服务端 prompt cache 预热：真实请求前先用同一稳定前缀发一条
    # max_tokens=1 的廉价请求。实测把缓存率从 0.2% 提到 100%，
    # 且不依赖对缓存 TTL 的猜测。设为 false 可关闭。
    prompt_cache_prime: bool = True

    # 出厂默认网关（2026-10-06 起）。原先指向 api.deepseek.com / deepseek-v4-flash，
    # 该服务不再使用；而 `config/settings.json` 在 .gitignore 中，clone 后根本没有，
    # 于是任何依赖代码默认值的路径都会连到一个无效端点。这里与 settings.json 保持
    # 一致，让「无配置时的兜底」也指向真实可用的网关。
    model: str = "space-bunny-free"
    base_url: str = "http://192.168.2.128:8093/v1"
    # 网关本身忽略鉴权，但 OpenAI SDK 要求 api_key 非空，否则构造客户端即抛错
    api_key: str = "not-needed"
    thinking: bool = True
    reasoning_effort: Literal["low", "medium", "high", "max"] = "high"
    context_window: int = 2_000_000
    #: Optional explicit override for max_tokens sent to provider. 0/None = auto
    #: (use per-provider default, see _provider_max_output_tokens). TODO P2.3.
    max_output_tokens: int | None = None
    #: 随机性控制（让 LLM 返回更稳定）：
    #:  - seed：同一输入+同一 seed 理论上返回相同结果（DeepSeek 官方不保证 100% 复现，
    #:    thinking 模式下效果更弱，但能显著降低波动）。None=不发送。
    #:  - top_p：核采样阈值，0~1。0.1=近似贪心（仅最高概率 token），1.0=完全随机。
    #:    thinking 模式下仍可使用（与 temperature 不同）。None=不发送（用 provider 默认 1.0）。
    seed: int | None = None
    top_p: float | None = None


class PromptSettings(BaseModel):
    """Prompt assembly tuning (accuracy-oriented defaults)."""
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    #: When True, Stage 2 loads every strategy .txt (legacy/test behaviour).
    stage2_load_full_strategy_library: bool = False
    # 0 = 不注入经验库。历史默认 0，等于整条经验库读取链路长期空跑；
    # 现默认 3 条，让已有 59 条经验真正参与 Stage2 提示词组装。
    experience_max_entries: int = Field(default=3, ge=0, le=10)
    # 计划在 TP1/SL 之一被触及时自动回写一条经验（闭环的写入端）
    experience_auto_write: bool = Field(default=True)
    # 单笔计划最长观察时长（秒）。超时仍未触及 TP/SL 则不写入 ——
    # 未了结的交易无法说明这个 setup 好不好。
    experience_max_wait_s: float = Field(default=86400.0, ge=300.0, le=604800.0)
    # 两阶段经验库：入场即写 pending，再按入场后的 N 根 K 线判定终态。
    # N 太小容易在噪音里误判 TP/SL 触及，太大则迟迟不结算。
    experience_verify_bars: int = Field(default=20, ge=3, le=500)
    # 一次验证最多处理多少条待验证记录（每条可能要单独拉一次行情）
    experience_verify_batch: int = Field(default=5, ge=1, le=50)
    # 后台自动结算的轮询间隔（秒）。下限 30，由 scheduler 强制。
    experience_verify_interval_s: float = Field(default=180.0, ge=30.0, le=3600.0)
    # 结算方式：
    #   "auto"  — 定时器自动结算（另有「验证」按钮可手动触发）
    #   "manual" — 只在用户点「验证」按钮时结算
    # 用 "manual" 的场景：需要人工逐条确认、或行情源不稳定时避免后台频繁取数
    experience_verify_mode: str = Field(default="auto", pattern="^(auto|manual)$")
    experience_max_chars_per_entry: int = Field(default=400, ge=100, le=4000)
    #: Inject pattern判定表 + 速查 brief into Stage 1 user prompt (reduces missed tags).
    stage1_inject_pattern_briefs: bool = True


class ValidationSettings(BaseModel):
    """Post-LLM validation behaviour."""
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    normalization_mode: NormalizationMode = "lenient"
    #: Stage-1 cross-field checks (gate trace, bar_by_bar, pattern tags). Off by default.
    stage1_coherence_checks: bool = False
    #: Stage-2 trace / diagnosis cross-checks (not order safety). Off by default.
    stage2_coherence_checks: bool = False
    trace_semantic_checks: bool = False
    strict_bar_by_bar_features: bool = False
    #: Allow Stage 1 truncated JSON tail repair before failing syntax validation.
    disable_truncation_repair: bool = False
    #: Re-call API with structured feedback when validation fails (format errors).
    retry_enabled: bool = True
    retry_max: int = Field(default=3, ge=0, le=5)
    #: Max retries for category=c semantic errors (subset only).
    retry_max_semantic: int = Field(default=1, ge=0, le=3)
    retry_stage2: bool = True


class GeneralSettings(BaseModel):
    """UI and data-feed general settings."""
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    analysis_bar_count: int = Field(default=100, ge=2, le=5000)
    refresh_interval_ms: int = 1000
    context_warning_threshold_pct: float = 99_999_999.0
    # 本仓库 Web 后端为 TradingView-only（见 data/factory.py::DATA_SOURCE_CHOICES，
    # UI 只暴露 tradingview）。上游 1.31 把此默认值改为 "mt5"，但 MT5 仅在
    # Windows 可用，Linux/Docker 下 create_data_source("mt5") 抛
    # DataSourceTransientError 且被 AppContext.bootstrap() 的 except 吞掉，
    # 导致全新部署静默启动但无任何数据源。此处保持 tradingview。
    last_data_source: DataSourceKind = "tradingview"
    #: A-share K-line adjust for East Money / Baostock (qfq=前复权)
    kline_adjust: Literal["qfq", "hfq", "none"] = "qfq"
    #: TradingView 交易所；空字符串 =（自动）依次探测预设列表
    last_tradingview_exchange: str = ""
    #: 同上：上游默认的 XAUUSDm 是 MT5 品种代码，Web 端改用 TradingView 黄金默认。
    last_symbol: str = "XAUUSD"
    last_timeframe: str = "15m"
    #: K 线图显示所用时区（IANA 名称，如 Asia/Shanghai）
    display_timezone: str = "Asia/Shanghai"
    decision_flow_auto_play: bool = True
    decision_flow_play_seconds: int = 50
    #: 阶段二给出限价/突破/市价单时：警报音、弹窗，并自动切到「决策」页（跳过决策树可视化演示）
    alert_on_order_opportunity: bool = True
    incremental_max_new_bars: int = Field(default=10, ge=0, le=500)
    #: 阶段二交易倾向：balanced=默认；conservative/aggressive 逐级调整下单意愿
    decision_stance: DecisionStance = "balanced"
    #: 决策树可视化：在「整图适配」基础上的缩放百分比（100=与适配一致；可任意放大，仅下限 10%）
    decision_flow_default_zoom_pct: int = Field(default=600, ge=10)
    #: 「实时」页思考过程/撰写回答框与追问输入框的等宽字体字号（pt）
    stream_pane_font_pt: int = Field(default=11, ge=8, le=28)
    #: K 线图上 #序号 标签的字号（pt）
    chart_seq_label_font_pt: int = Field(default=11, ge=6, le=24)
    #: 两阶段分析结束后是否自动恢复 K 线图表实时刷新
    auto_resume_chart_after_analysis: bool = False
    #: 持续跟踪分析：有新K线收盘时自动触发新一轮分析
    keep_analysis: bool = False
    #: 重试后取消持续跟踪分析：校验失败触发重试后自动关闭 keep_analysis
    cancel_keep_analysis_on_retry: bool = False
    #: 交易决策置信度门槛：仅当 trade_confidence >= 此值时，才视为有下单机会（弹窗警报并提供决策详情）
    decision_confidence_threshold: int = Field(default=40, ge=0, le=100)
    #: 开启下根K线预期功能；关闭时不向模型请求该预测，节省 token
    enable_next_bar_prediction: bool = False
    #: 同一结构位 entry 相差≤3跳时，禁止反向新方案的冷却 K 线根数（已收盘）
    structure_flip_cooldown_bars: int = Field(default=3, ge=1, le=50)

    @field_validator("last_data_source", mode="before")
    @classmethod
    def _coerce_legacy_data_source(cls, v: object) -> object:
        if v == "yfinance":
            return "eastmoney"
        if v in ("adata", "a_share"):
            return "akshare"
        if v == "eastmoney":
            return "eastmoney"
        if v == "tushare":
            return "tushare"
        return v

    @field_validator("decision_flow_default_zoom_pct", mode="before")
    @classmethod
    def _coerce_zoom_pct(cls, v: object) -> object:
        if v is None:
            return 50
        return v


_FEISHU_CONFIG_KEYS = (
    "enabled",
    "webhook_url",
    "secret",
    "app_id",
    "app_secret",
    "notify_on_order_only",
)


class FeishuSettings(BaseModel):
    """Feishu bot notification settings (persisted in settings.json)."""
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    enabled: bool = True
    webhook_url: str = ""
    secret: str = ""
    app_id: str = ""
    app_secret: str = ""
    #: True = only push when there is an order opportunity.
    notify_on_order_only: bool = True


class TushareSettings(BaseModel):
    """Tushare Pro data source settings (persisted in ignored settings.json)."""
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    token: str = ""


class TradingViewSettings(BaseModel):
    """TradingView credentials persisted in settings.json.

    Three authentication modes are supported, checked in order:
    1. session_id: Direct cookie-based auth (most stable, avoids recaptcha).
       Extract the `sessionid` cookie from a logged-in browser session.
    2. username + password: Traditional login via /accounts/signin/.
       May trigger TradingView's recaptcha risk control.
    3. Anonymous mode: No credentials provided. Rate-limited for US equities.

    Env vars ``PA_AGENT_TRADINGVIEW_SESSION_ID`` / ``PA_AGENT_TRADINGVIEW_USERNAME``
    / ``PA_AGENT_TRADINGVIEW_PASSWORD`` override these on a per-deployment basis.
    """
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    username: str = ""
    password: str = ""
    session_id: str = ""


class PushPlusSettings(BaseModel):
    """PushPlus notification settings (settings.json only; no GUI)."""
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    enabled: bool = False
    token: str = ""


class Settings(BaseModel):
    """Root settings object persisted to config/settings.json."""
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    provider: AIProviderSettings = Field(default_factory=AIProviderSettings)
    general: GeneralSettings = Field(default_factory=GeneralSettings)
    prompt: PromptSettings = Field(default_factory=PromptSettings)
    validation: ValidationSettings = Field(default_factory=ValidationSettings)
    feishu: FeishuSettings = Field(default_factory=FeishuSettings)
    pushplus: PushPlusSettings = Field(default_factory=PushPlusSettings)
    tushare: TushareSettings = Field(default_factory=TushareSettings)
    tradingview: TradingViewSettings = Field(default_factory=TradingViewSettings)


def provider_api_key_configured(settings: Settings | None) -> bool:
    """Return True when a non-empty API key is loaded in memory."""
    if settings is None:
        return False
    return bool((settings.provider.api_key or "").strip())


# ── Persistence ───────────────────────────────────────────────────────────────
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _migrate_legacy_feishu_json(raw: dict, settings_path: Path) -> bool:
    """Merge legacy config/feishu.json into settings.feishu when needed."""
    legacy_path = settings_path.parent / "feishu.json"
    if not legacy_path.exists():
        return False

    feishu = raw.setdefault("feishu", {})
    if (feishu.get("webhook_url") or "").strip():
        return False

    try:
        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("legacy feishu.json unreadable (%s); skipping migration", exc)
        return False

    migrated = False
    for key in _FEISHU_CONFIG_KEYS:
        if key not in legacy:
            continue
        value = legacy.get(key)
        if value in (None, ""):
            continue
        if feishu.get(key) in (None, ""):
            feishu[key] = value
            migrated = True
    if migrated:
        logger.info("Migrated Feishu config from %s into settings.json", legacy_path)
    return migrated


def normalize_raw(raw: dict) -> dict:
    """Legacy 字段迁移：**纯函数**，只做「改名 + 黄金默认值修正」。

    从 :func:`_load_settings_from_file` 里抽出来的独立段，因为 **DB 路径也必须跑它**：
    ``_try_load_from_db`` 曾把文件 raw 原样交给 ``resolve()`` → ``save_baseline()``，
    于是 ``default_bar_count`` / ``cost_warning_threshold_pct`` 这些旧字段名直接被
    升格成系统兜底 —— 之后每次启动都解析不出真值，迁移段被彻底绕过。

    **为什么必须留在 config 层、不能下沉到 storage 层**：它依赖
    ``pa_agent.data.market_defaults``，而 storage 层一旦 import 它，
    ``data.market_defaults`` → ``data.tradingview`` → tvDatafeed 整条会被拉起来 ——
    纯配置读路径从此再也起不来（Docker 镜像里 tvDatafeed 还可能是可选依赖）。

    纯函数、无 I/O、无环境变量读取：需要文件路径或环境变量的那部分在
    :func:`_repair_file_side`。
    """
    out = dict(raw or {})
    general = out.get("general")
    general = dict(general) if isinstance(general, dict) else {}
    if "cost_warning_threshold_pct" in general and "context_warning_threshold_pct" not in general:
        general["context_warning_threshold_pct"] = general.pop("cost_warning_threshold_pct")
    general.pop("last_htf_text", None)
    from pa_agent.data.market_defaults import migrate_general_gold_defaults

    migrate_general_gold_defaults(general)
    if "default_bar_count" in general and "analysis_bar_count" not in general:
        general["analysis_bar_count"] = general.pop("default_bar_count")
    out["general"] = general

    provider = out.get("provider")
    provider = dict(provider) if isinstance(provider, dict) else {}
    # 「Migrate legacy encrypted key: drop it, api_key already in provider dict」
    provider.pop("pricing", None)
    provider.setdefault("api_key", "")
    out["provider"] = provider
    return out


def _repair_file_side(raw: dict, path: Path) -> bool:
    """需要**文件路径 / 环境变量**的那部分迁移，就地改 *raw*，返回是否改动过。

    与 :func:`normalize_raw` 分开的原因只有一个：这两个输入 storage 层拿不到也不该拿
    —— 飞书 legacy 配置在 ``settings.json`` **同目录**（得知道 path 才能找），
    PushPlus 互锁要看进程环境里的 ``PUSHPLUS_TOKEN``。两条加载路径都跑这两段。
    """
    dirty = _migrate_legacy_feishu_json(raw, path)
    pushplus = raw.get("pushplus")
    # 段缺失时 Settings 默认 enabled=False，本就无需互锁，故只在段存在时处理
    if isinstance(pushplus, dict) and pushplus.get("enabled"):
        empty = not str(pushplus.get("token") or "").strip()
        if empty and not (os.environ.get("PUSHPLUS_TOKEN") or "").strip():
            pushplus["enabled"] = False
            logger.info(
                "PushPlus enabled but token empty — auto-disabled "
                "(Feishu notifications unaffected)"
            )
            dirty = True
    return dirty


def load_settings(path: Path | None = None, *, user_id: str | None = None) -> "Settings":
    """Load settings from *path* (default: SETTINGS_JSON_PATH).

    Returns default Settings and writes them to disk if the file is absent.

    **真源在 DB（用户级 admin）**，本函数是 DB 优先、文件回退的入口：

    - DB 有配置 → 用它（用户在 UI 里的修改优先于文件）
    - DB 无配置但文件有 → 用文件，并**首次**把内容导入 DB（迁移）
    - DB 不可用/损坏 → 纯文件（降级路径）

    只对默认路径启用 DB 层：``path != SETTINGS_JSON_PATH`` 说明调用方要的是
    隔离的文件（测试用临时路径），此时不得碰真实 DB，否则测试会互相污染。

    ## ``user_id``：按请求解析

    ``user_id=None`` 保留原语义 —— 走 :func:`~pa_agent.storage.users.default_user_id`
    （启动时的默认解析），供**无请求上下文**的后台线程 / 调度器 / CLI 使用。
    请求路径必须显式传入 :func:`web.api.auth_ctx.current_user_id` 的结果，
    否则所有登录用户会共用同一份配置（这正是本参数存在的原因）。
    """
    from pa_agent.config.paths import SETTINGS_JSON_PATH

    using_real = path is None or path == SETTINGS_JSON_PATH
    path = path or SETTINGS_JSON_PATH

    if using_real:
        override = _try_load_from_db(user_id=user_id)
        if override is not None:
            return override

    return _load_settings_from_file(path)


def _try_load_from_db(user_id: str | None = None) -> "Settings | None":
    """按级联解析有效配置：系统兜底 ← 用户覆盖（文件仅作首次播种与灾备兜底）。

    返回 None 表示 DB 与文件都没有配置，调用方回退纯文件逻辑。
    任何异常都吞掉并返回 None —— 索引层故障绝不能阻断启动。

    走 :func:`~pa_agent.storage.settings_store.resolve_cached` 而非裸
    ``resolve()``：请求路径每个 HTTP 请求都会调到这里，裸 resolve 的两次 DB 读
    加上 settings.json 的文件读会让 5 秒一次的 ``/api/bars`` 轮询明显变慢。
    """
    try:
        from pa_agent.config.paths import SETTINGS_JSON_PATH
        from pa_agent.storage.settings_store import resolve_cached
        from pa_agent.storage.users import default_user_id

        uid = user_id or default_user_id()

        def _file_fallback() -> dict | None:
            """惰性读播种源 —— 缓存命中时**一次文件 IO 都不发生**。"""
            try:
                if not SETTINGS_JSON_PATH.exists():
                    return None
                raw = json.loads(SETTINGS_JSON_PATH.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    return None
                # **legacy 迁移必须先于播种**（B2）。曾把 raw 原样交给 resolve()
                # → save_baseline()，旧字段名被直接固化成系统兜底，且此后
                # 每次启动都读它 —— 迁移段被完整绕过。
                out = normalize_raw(raw)
                _repair_file_side(out, SETTINGS_JSON_PATH)
                return out
            except (json.JSONDecodeError, OSError):
                return None
            except Exception as exc:  # noqa: BLE001 - 迁移失败退回纯文件路径，不阻断启动
                logger.warning("settings.json legacy 迁移失败，退回纯文件路径: %s", exc)
                return None

        data = resolve_cached(uid, _file_fallback)
        if data is None:
            return None
        return Settings.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("DB-backed settings load failed, using file: %s", exc)
        return None


#: user_id -> (代数, 时刻, **已盖过 .env** 的配置 dict)
#:
#: 为什么要单独一层：``apply_env_overrides`` 实测 ~41µs/次（15 个字段的 setattr
#: 加上每次都重新 stat/读 ``.env``），比 ``Settings.model_validate`` 还贵。把它留在
#: 每请求路径上会让「按请求解析」比改造前的 ``load_settings()`` **更慢**（实测
#: 0.107ms vs 0.051ms）—— 配置变按请求解析是**性能倒退**。
#:
#: 环境变量在进程生命周期内是常量，因此「盖一次、缓存结果」与「每次盖一遍」
#: 语义完全等价，却把热路径压回 ~32µs（低于改造前）。
#:
#: 缓存的仍是 dict 而非 ``Settings``：``PUT /api/settings`` 会就地 ``setattr``
#: 改字段，共享实例必然导致「A 改完 B 跟着变」。每次 ``model_validate`` 出一份
#: 独立实例，别名问题从根上不存在。
_EFFECTIVE_CACHE: dict[str, tuple[int, str, float, dict[str, Any]]] = {}
_EFFECTIVE_LOCK = threading.Lock()


def _effective_cache_get(user_id: str) -> dict[str, Any] | None:
    """命中返回「已盖 .env 的配置 dict」；代数/库/TTL 任一不符即重算。

    返回 None 永远是「正常未命中」，**不是错误**。注意本函数**绝不抛异常**：
    缓存是纯优化，它坏掉只能让请求变慢，绝不能变成 500。
    """
    try:
        from pa_agent.storage.settings_store import (
            _CACHE_TTL_S,
            hub_token,
            settings_generation,
        )

        token = hub_token()
        with _EFFECTIVE_LOCK:
            entry = _EFFECTIVE_CACHE.get(user_id)
            if entry is None:
                return None
            generation, hub, stamp, data = entry
            if generation != settings_generation() or hub != token:
                _EFFECTIVE_CACHE.clear()   # 代数推进或换了库：整份作废
                return None
            if (time.monotonic() - stamp) > _CACHE_TTL_S:
                _EFFECTIVE_CACHE.clear()
                return None
            return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("生效配置缓存读取失败，本次按未命中处理: %s", exc)
        return None


def _effective_cache_put(user_id: str, data: dict[str, Any]) -> None:
    try:
        from pa_agent.storage.settings_store import hub_token, settings_generation

        with _EFFECTIVE_LOCK:
            _EFFECTIVE_CACHE[user_id] = (
                settings_generation(), hub_token(), time.monotonic(), data,
            )
    except Exception as exc:  # noqa: BLE001
        # **warning 而非 debug**：本函数静默失败过一次（局部 import 遮蔽了模块
        # 全局名，NameError 被 try/except 吃掉），表现为「每请求都重算」——
        # 功能全对、热路径慢 3 倍，且没有任何报错。缓存写失败必须看得见。
        logger.warning("生效配置写入缓存失败（请求仍正确，但会变慢）: %s", exc)


def resolve_effective_settings(user_id: str) -> "Settings":
    """按 *user_id* 解析**本次请求生效**的完整配置（含 .env 覆盖）。

    这是请求路径上「这个用户此刻该用什么配置」的唯一答案，由
    ``web/server.py`` 的中间件调用一次并绑定进 ContextVar。相对裸
    :func:`load_settings` 多做两件事，二者都不可省：

    1. **按 user_id 解析级联**（系统兜底 ← 本人覆盖）—— 否则所有登录用户
       共用同一份配置。
    2. **``apply_env_overrides``** —— 启动时 ``AppContext.bootstrap()`` 会对
       配置就地盖一遍 .env（API_KEY / BASE_URL 等）。若请求路径不盖，
       默认用户拿得到凭证、其他用户拿到的却是空 api_key，同一份配置出现两种
       形态。函数幂等（只覆盖「环境变量确实设置了」的字段）。

    **每次返回独立实例**：见 :data:`_EFFECTIVE_CACHE` 的说明。

    解析失败时回落到纯文件 / 代码默认值，保证配置层故障时请求仍能拿到一份可用
    配置 —— 与 :func:`resolve` 的降级方向一致。任何异常都不外抛。
    """
    from pa_agent.config.env_loader import apply_env_overrides
    from pa_agent.config.paths import SETTINGS_JSON_PATH

    hit = _effective_cache_get(user_id)
    if hit is not None:
        return Settings.model_validate(hit)

    try:
        settings = _try_load_from_db(user_id=user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("按用户解析配置失败: %s", exc)
        settings = None
    if settings is None:
        # DB 与播种源都没有配置 → 纯文件 / 代码默认值
        try:
            settings = _load_settings_from_file(SETTINGS_JSON_PATH)
        except Exception as exc:  # noqa: BLE001
            logger.warning("settings.json 回落读取失败，改用代码默认值: %s", exc)
            settings = Settings()
    apply_env_overrides(settings)
    _effective_cache_put(user_id, settings.model_dump())
    return settings


def _load_settings_from_file(path: Path) -> "Settings":

    if not path.exists():
        defaults = Settings()
        save_settings(defaults, path)
        return defaults

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("settings.json unreadable (%s); using defaults", exc)
        return Settings()

    # Migrate legacy field names（纯函数段，与 DB 路径共用同一份实现）
    raw = normalize_raw(raw)

    # 依赖文件路径 / 环境变量的那一段
    dirty = _repair_file_side(raw, path)
    settings = Settings.model_validate(raw)
    if dirty:
        save_settings(settings, path)
    return settings


def save_settings(settings: "Settings", path: Path | None = None) -> None:
    """Persist settings to *path* (default: SETTINGS_JSON_PATH)."""
    from pa_agent.config.paths import SETTINGS_JSON_PATH

    path = path or SETTINGS_JSON_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    data = settings.model_dump()

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 级联写入：用户层 patch ─────────────────────────────────────────────────────
#
# 唯一允许的「写配置」入口形态。**收 patch，不收整份 Settings** —— 原因见
# :func:`persist_patch`。系统兜底层（global_config）在这里不可写：它是所有用户的
# 只读默认值，只有播种（首次把 settings.json 升格进去）才允许动它。

#: connector / 自动 fallback 有权改写的 provider 字段。其余（thinking /
#: reasoning_effort / seed / top_p / max_output_tokens …）是用户偏好或 .env，
#: connector 碰了等于替用户做决定。
_CONNECTOR_PROVIDER_FIELDS = ("model", "base_url", "api_key", "context_window")


def persist_patch(patch: dict, *, user_id: str | None = None) -> bool:
    """把*调用方自己改的那几个键*写进用户层（user_prefs），返回是否落库成功。

    **为什么不能收整份 settings**：内存里的对象已经被 ``apply_env_overrides``
    盖过一遍环境变量（15 个字段，含 API_KEY / BASE_URL / model）。整份算 diff
    会把 .env 的值永久烧进用户层 —— 此后用户改 .env 不再生效，删掉 .env 也
    「配置被莫名重置」，且系统兜底对这些字段的任何后续更新都传不到他身上。

    只声明自己改的键，则用户层永远是稀疏的：没声明的字段继续继承系统兜底。

    写入仍是「合并后再按基准稀疏化」：与基准取值相同的键不会留在用户层里
    （否则用户把某个字段改回默认值后，它会永久挡住系统兜底的后续更新）。
    系统兜底层**绝不被触碰**。

    不写 settings.json：那份文件只是灾备副本，且 connector 每次启动都会重新探测
    并重放一遍，落不落盘无所谓；而在这里写文件等于把「resolve 读不出来 → 用默认
    Settings 覆写用户配置」变成一个随时可触发的数据丢失路径。

    读失败时拒绝写入（同 :func:`~pa_agent.storage.settings_store.apply_user_change`）：
    基准读不出来时算出的 diff 没有意义，那会把整份默认值固化成用户覆盖。
    """
    try:
        from pa_agent.storage.db import get_hub
        from pa_agent.storage.settings_store import (
            compute_diff,
            load_baseline,
            load_overrides,
            merge_overrides,
            save_overrides,
        )
        from pa_agent.storage.users import default_user_id

        if not patch:
            return True

        hub = get_hub()
        if hub.read_failed:
            logger.error(
                "拒绝写入用户覆盖（%s）：DB 读取失败（%s）。本次改动只留在内存。",
                ",".join(sorted(patch)) or "<empty>",
                hub.read_error,
            )
            return False

        uid = user_id or default_user_id()
        layer = merge_overrides(load_overrides(uid), patch)
        return save_overrides(compute_diff(load_baseline() or {}, layer), uid)
    except Exception as exc:  # noqa: BLE001 - 落库失败绝不冒泡进业务流
        logger.warning("persist_patch failed (%s): %s", patch, exc)
        return False


def persist_provider(provider: Any) -> bool:
    """connector / 自动 fallback 落库的唯一出口（8 条 provider 写路径归零到此）。

    只写 :data:`_CONNECTOR_PROVIDER_FIELDS` 那四个字段 —— connector 本来也只改
    这四个（model / base_url / api_key / context_window）。写整段 provider 会把
    ``apply_env_overrides`` 盖上的 thinking / seed / top_p 一并固化，等于让
    connector 替用户决定了偏好。
    """
    values = {
        field: getattr(provider, field)
        for field in _CONNECTOR_PROVIDER_FIELDS
        if getattr(provider, field, None) is not None
    }
    return persist_patch({"provider": values})
