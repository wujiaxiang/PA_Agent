# 本地配置说明

本目录下的**运行时文件**默认已被 `.gitignore` 忽略，不会进入 Git 仓库。

仓库同样**不会上传**：`records/`（分析落盘）、`experience/`（经验库内容）、`logs/`、`trade_records/`（交易 CSV/截图）、`.env`、根目录临时图片与个人笔记等。仅源代码、`prompt_engineering/` 策略文本、`tests/` 与 `docs/` 说明文档会进入 GitHub。

## 相关文档

- [README.md](../README.md) — 项目总览、快速开始、三层覆盖优先级说明
- [.env.example](../.env.example) — 环境变量模板（与 `settings.json` 配合使用）
- [PA_Agent使用文档.md](../docs/PA_Agent使用文档.md) — 完整功能说明
- [CONTRIBUTING.md](../CONTRIBUTING.md) — 开发者环境搭建

## 首次使用

1. 复制模板为本地配置：

   ```cmd
   copy config\settings.example.json config\settings.json
   ```

2. 启动程序，在 **设置** 中填写你的 **API Key**。网页保存走 `PUT /api/settings`，
   **只写数据库的 `overrides` 用户区**，不改本文件（见下节字段表的提示）。

   也可直接编辑 `config/settings.json` 中的 `base_url`、`model` 等字段——但只在
   **数据库还没有 baseline**（首次启动）时起作用；之后 DB 优先，文件仅作灾备兜底。

3. `config/exception_state.json` 由程序在需要时自动创建，一般无需手动复制。结构可参考 `exception_state.example.json`。

4. 如需自定义 TradingView 品种别名，复制模板：

   ```cmd
   copy config\tv_symbol_aliases.example.json config\tv_symbol_aliases.json
   ```

## `settings.json` 字段说明

配置分为四个顶层组：`provider`、`general`、`prompt`、`validation`。

### provider — AI 提供商

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
> ⚠️ **配置真源在 DB，不在本文件**：网页「设置」保存只写 SQLite 的
> `overrides` 用户区，`config/settings.json` 只是**首启播种源 + 灾备兜底**。
> 想直接编辑本文件生效，前提是 DB 里还没有 baseline（否则 DB 覆盖文件）。
> 想把当前生效配置变成「出厂默认」，用 `POST /api/settings/promote-default`。
> 下表默认值取自 `AIProviderSettings`（出厂兜底），与本文件、DB baseline 三处保持一致。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `provider.use_custom` | bool | `false` | **是否用自定义 LLM 配置**。为 `false` 时 `_apply_llm_source()` 会把用户对 provider 的覆盖**整段作废**（"跟随系统默认"）—— 只写 `model`/`base_url` 而不写这个开关，改动会被**静默丢弃、无任何报错** |
| `provider.model` | string | `"space-bunny-free"` | 模型名称（须与网关支持的名称一致）。改前先 `GET {base_url}/models` 核对——不同网关命名不同 |
| `provider.base_url` | string | `"http://192.168.2.128:8093/v1"` | OpenAI 兼容 API 根地址（内网网关）。`https://api.deepseek.com` 仍可用但需自行配置 key |
| `provider.api_key` | string | `"not-needed"` | API Key。**默认非空**：网关本身忽略鉴权，但 OpenAI SDK 要求非空，否则构造客户端即抛错 |
| `provider.prompt_cache_prime` | bool | `true` | 真实请求前用同一前缀先发 `max_tokens=1` 预热 prompt cache（实测命中率 0.2% → 100%） |
| `provider.thinking` | bool | `true` | 是否启用思考/推理类扩展参数（依模型与网关而定）。关闭可 3–5 倍提速但分析质量下降 |
| `provider.reasoning_effort` | string | `"high"` | 推理深度：`low` / `medium` / `high` / `max` |
| `provider.context_window` | int | `2000000` | 用于上下文占用提示的窗口大小（tokens） |
| `provider.max_output_tokens` | int / null | `null` | 覆盖单次响应最大 tokens。`null` = 按 provider 默认值；free 模型建议设 32768 避免被拒 |
| `provider.seed` | int | `null` | 随机性控制种子。同一输入+同一 seed 理论上返回相同结果（DeepSeek 官方不保证 100% 复现，thinking 模式下效果更弱，但能显著降低波动）。`null` = 不发送 |
| `provider.top_p` | float | `null` | 核采样阈值 0~1。`0.1` = 近似贪心（仅最高概率 token），`1.0` = 完全随机。与 `temperature` 不同，`top_p` 在 thinking 模式下仍可使用。`null` = 不发送（用 provider 默认 1.0）。**推荐 0.1** |

### general — 通用设置

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `general.last_data_source` | string | `"tradingview"` | K 线数据来源。UI 仅展示 `tradingview`；`mt5` / `akshare` / `eastmoney` / `tushare` / `yfinance` 仍代码支持 |
| `general.last_tradingview_exchange` | string | `"GATEIO"` | TradingView 交易所。空字符串 = 自动探测。加密货币常用：`GATEIO` / `BINANCE` / `BYBIT` / `OKX`；外汇黄金：`OANDA` / `PEPPERSTONE`；A股：`SSE` / `SZSE` |
| `general.last_symbol` | string | `"BTCUSDT"` | 默认品种。TradingView 用标准名：加密 `BTCUSDT`、黄金 `XAUUSD`、A股 `600519`、港股 `0700`。MT5 需含后缀（如 `XAUUSDm`） |
| `general.last_timeframe` | string | `"15m"` | 默认周期，如 `1m`、`5m`、`15m`、`1h`、`4h`、`1d` |
| `general.analysis_bar_count` | int | `100` | 提交分析时使用的 K 线数量（2–5000） |
| `general.refresh_interval_ms` | int | `1000` | 图表自动刷新间隔（毫秒） |
| `general.context_warning_threshold_pct` | float | `80.0` | 上下文占用警告阈值（百分比） |
| `general.decision_stance` | string | `"balanced"` | 阶段二交易倾向：`conservative` / `balanced` / `aggressive` / `extreme_aggressive` |
| `general.incremental_max_new_bars` | int | `10` | 增量分析触发阈值：新增已收盘 K 线 ≤ 此值时自动走增量模式（0–500） |
| `general.auto_resume_chart_after_analysis` | bool | `false` | 分析结束后是否自动恢复「图表实时更新」 |
| `general.keep_analysis` | bool | `false` | 持续跟踪分析：新 K 线收盘时自动触发新一轮分析 |
| `general.cancel_keep_analysis_on_retry` | bool | `false` | 校验失败触发重试后自动关闭 `keep_analysis` |
| `general.alert_on_order_opportunity` | bool | `true` | 阶段二给出交易方案时播放警报音、弹窗提示，并自动切换到「决策」页 |
| `general.decision_flow_auto_play` | bool | `true` | 决策树可视化自动播放 |
| `general.decision_flow_play_seconds` | int | `50` | 决策树可视化自动播放时长（秒） |
| `general.decision_flow_default_zoom_pct` | int | `600` | 决策树可视化默认缩放百分比（≥10） |
| `general.stream_pane_font_pt` | int | `11` | 「实时」页等宽字体字号（pt，8–28） |
| `general.chart_seq_label_font_pt` | int | `11` | K 线图上序号标签的字号（pt，6–24） |

### prompt — Prompt 组装调优

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `prompt.stage2_load_full_strategy_library` | bool | `false` | 阶段二是否加载全部 22 个策略文件（通常仅路由匹配的策略文件） |
| `prompt.experience_max_entries` | int | `3` | 经验库最大加载条目数（0–10） |
| `prompt.experience_max_chars_per_entry` | int | `400` | 每条经验最大字符数（100–4000） |
| `prompt.stage1_inject_pattern_briefs` | bool | `true` | 阶段一是否注入模式判定表和速查 brief（减少 missed tags） |

### validation — 校验与重试

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `validation.normalization_mode` | string | `"lenient"` | 归一化模式：`strict`（严格拒绝异常值）/ `lenient`（容忍轻微偏差） |
| `validation.stage1_coherence_checks` | bool | `false` | 阶段一跨字段一致性检查（闸门 trace、逐 K 摘要、模式标签等） |
| `validation.stage2_coherence_checks` | bool | `false` | 阶段二诊断与 trace 交叉检查 |
| `validation.trace_semantic_checks` | bool | `false` | 语义一致性检查（方向/信号逻辑冲突检测） |
| `validation.strict_bar_by_bar_features` | bool | `false` | 严格逐 K 特征校验（开启后对特征字段做严格验证） |
| `validation.disable_truncation_repair` | bool | `false` | 禁用流式 JSON 截断尾部修复 |
| `validation.retry_enabled` | bool | `true` | 校验失败时是否自动重试 |
| `validation.retry_max` | int | `3` | 格式错误（category a）最大重试次数（0–5） |
| `validation.retry_max_semantic` | int | `1` | 语义错误（category c）最大重试次数（0–3） |
| `validation.retry_stage2` | bool | `true` | 阶段二校验失败时是否重试 |

## 安全提醒

- **不要**将 `config/settings.json`、`config/exception_state.json`、`config/tv_symbol_aliases.json` 提交到 Git。
- 若曾误提交 API Key，请立即在服务商处**作废并轮换**密钥。
- 建议在仓库根目录执行：`powershell -ExecutionPolicy Bypass -File tools\setup_git_secrets.ps1`
