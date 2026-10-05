"""Pydantic settings models for PA Agent."""
from __future__ import annotations
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DecisionStance = Literal["conservative", "balanced", "aggressive", "extreme_aggressive"]
DataSourceKind = Literal["mt5", "tradingview", "akshare", "eastmoney", "eastmoney_futures", "tushare"]
NormalizationMode = Literal["strict", "lenient"]


class AIProviderSettings(BaseModel):
    """AI provider connection and behaviour settings."""
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    # 服务端 prompt cache 预热：真实请求前先用同一稳定前缀发一条
    # max_tokens=1 的廉价请求。实测把缓存率从 0.2% 提到 100%，
    # 且不依赖对缓存 TTL 的猜测。设为 false 可关闭。
    prompt_cache_prime: bool = True

    model: str = "deepseek-v4-flash"
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    api_key_encrypted: str = ""
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
from pathlib import Path

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


def load_settings(path: Path | None = None) -> "Settings":
    """Load settings from *path* (default: SETTINGS_JSON_PATH).

    Returns default Settings and writes them to disk if the file is absent.

    **真源在 DB（用户级 admin）**，本函数是 DB 优先、文件回退的入口：

    - DB 有配置 → 用它（用户在 UI 里的修改优先于文件）
    - DB 无配置但文件有 → 用文件，并**首次**把内容导入 DB（迁移）
    - DB 不可用/损坏 → 纯文件（降级路径）

    只对默认路径启用 DB 层：``path != SETTINGS_JSON_PATH`` 说明调用方要的是
    隔离的文件（测试用临时路径），此时不得碰真实 DB，否则测试会互相污染。
    """
    from pa_agent.config.paths import SETTINGS_JSON_PATH

    using_real = path is None or path == SETTINGS_JSON_PATH
    path = path or SETTINGS_JSON_PATH

    if using_real:
        override = _try_load_from_db()
        if override is not None:
            return override

    return _load_settings_from_file(path)


def _try_load_from_db() -> "Settings | None":
    """按级联解析有效配置：系统兜底 ← 用户覆盖（文件仅作首次播种与灾备兜底）。

    返回 None 表示 DB 与文件都没有配置，调用方回退纯文件逻辑。
    任何异常都吞掉并返回 None —— 索引层故障绝不能阻断启动。
    """
    try:
        from pa_agent.config.paths import SETTINGS_JSON_PATH
        from pa_agent.storage.settings_store import resolve
        from pa_agent.storage.users import default_user_id

        file_fallback = None
        try:
            if SETTINGS_JSON_PATH.exists():
                raw = json.loads(SETTINGS_JSON_PATH.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    file_fallback = raw
        except (json.JSONDecodeError, OSError):
            file_fallback = None

        data = resolve(default_user_id(), file_fallback)
        if data is None:
            return None
        return Settings.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("DB-backed settings load failed, using file: %s", exc)
        return None


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

    # Migrate legacy field names
    general = raw.get("general", {})
    if "cost_warning_threshold_pct" in general and "context_warning_threshold_pct" not in general:
        general["context_warning_threshold_pct"] = general.pop("cost_warning_threshold_pct")
    general.pop("last_htf_text", None)
    from pa_agent.data.market_defaults import migrate_general_gold_defaults

    migrate_general_gold_defaults(general)
    if "default_bar_count" in general and "analysis_bar_count" not in general:
        general["analysis_bar_count"] = general.pop("default_bar_count")
    raw["general"] = general
    provider = raw.get("provider", {})
    provider.pop("pricing", None)
    raw["provider"] = provider

    # Migrate legacy encrypted key: drop it, api_key already in provider dict
    raw.setdefault("provider", {}).setdefault("api_key", "")

    migrated_feishu = _migrate_legacy_feishu_json(raw, path)
    settings = Settings.model_validate(raw)
    dirty = migrated_feishu
    if settings.pushplus.enabled and not settings.pushplus.token.strip():
        if not (os.environ.get("PUSHPLUS_TOKEN") or "").strip():
            settings.pushplus.enabled = False
            logger.info(
                "PushPlus enabled but token empty — auto-disabled "
                "(Feishu notifications unaffected)"
            )
            dirty = True
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
