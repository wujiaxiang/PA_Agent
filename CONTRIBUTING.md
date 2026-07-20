# 参与贡献

感谢你对 PA Agent 的关注。本项目欢迎 Issue 与 Pull Request。

> 本仓库是 [原项目](https://github.com/rosemarycox5334-debug/PA_Agent) 的二次开发版本，由 [wujiaxiang](https://github.com/wujiaxiang) 维护。**不提供原项目的用户支持、交流群与打赏入口**；问题反馈请走 GitHub Issues。

## 相关文档

- [README.md](README.md) — 项目总览、快速开始、架构概览、13 条已知坑库
- [TODO.md](TODO.md) — 后续优化路线图（本文件是 TODO 的开发约束补充）
- [AGENTS.md](AGENTS.md) — 项目改动日志（大改动必须同步追加到 AGENTS）
- [PA_Agent使用文档.md](PA_Agent使用文档.md) — 完整功能说明
- [config/README.md](config/README.md) — `settings.json` 全字段说明
- [docs/](docs/) — 技术细节文档（K 线序号、数据获取流程）

## 开发环境

1. Windows 10/11 / macOS / Linux，Python 3.11+
2. （可选）安装 MetaTrader 5 并登录，用于 MT5 数据源真实 K 线联调；Web 后端默认使用 TradingView / AkShare，无需 MT5
3. 克隆仓库后：

   ```cmd
   python -m venv .venv
   .venv\Scripts\activate
   pip install -e ".[dev]"
   copy config\settings.example.json config\settings.json
   copy .env.example .env
   ```

4. 在 `.env` 中填入 `PA_AGENT_PROVIDER_API_KEY`、`PA_AGENT_PROVIDER_BASE_URL`、`PA_AGENT_PROVIDER_MODEL` 等（三层覆盖优先级：shell env > `.env` > `config/settings.json`，详见 [`.env.example`](.env.example)）
5. （可选）若需 TradingView 非加密交易所（HKEX / NYSE / NASDAQ 等），执行：

   ```bash
   pip install git+https://github.com/rongardF/tvdatafeed.git
   ```

   若遇到 NumPy CPU 不兼容（`X86_V2` 错误），降级到 `numpy==1.26.4`。A股数据源（AkShare）无需 tvDatafeed，可作 fallback。

## 启动方式

本仓库**默认推荐 Web 后端**模式（二次开发新增），桌面 GUI 仍可用但与 Web 后端是独立进程，**不要同时启动两个**。

### Web 后端（推荐）

```bash
python -m uvicorn web.server:app --host 0.0.0.0 --port 8000
# Windows 也可双击 start_pa_agent.bat
```

启动后访问 http://localhost:8000/，通过 `/api/health/check` 验证模型 API 与数据源连通性。

### 桌面 GUI（原项目主入口）

```bash
python -m pa_agent.main
```

启动后在 GUI **设置** 中配置 API Key（写入 `config/settings.json`）。需 PyQt6 与显示器，云端 / 无 GUI 环境不可用。

两种模式的详细差异见 [README.md](README.md#web-后端-vs-桌面-gui-的差异重要)。

## 提交代码前

```cmd
pytest -m "not e2e"
ruff check pa_agent tests
black --check pa_agent tests
```

- line-length 100，target py311
- 测试分层：`tests/unit`（快）/ `tests/property`（hypothesis）/ `tests/integration` / `tests/e2e`
- `live` 标记的测试必须通过环境变量配置 API key，**绝不读取** `config/settings.json`

## 请勿提交

- 配置与密钥：`config/settings.json`、`config/exception_state.json`、`.env`、任何 API Key 文件
- 运行时数据：`logs/`、`records/pending/`、`experience/` 下的内容
- 临时调试文件：`.tmp_*.json`、`dogfood-output/`、`tvdatafeed-main/`、`*.whl`、`tvdatafeed.zip`、`install_*.py` 等（详见 `.gitignore`）
- 个人分析记录：`.tmp_record*.json`（每次分析都会重新生成，无保留价值）

启用本地 pre-commit 钩子（git-secrets 扫描密钥泄漏）：

```powershell
powershell -ExecutionPolicy Bypass -File tools\setup_git_secrets.ps1
```

## Pull Request 建议

- 一个 PR 聚焦一类改动（功能 / 修复 / 文档）
- 说明动机与测试方式
- 若改 JSON schema、提示词或路由，请补充或更新 `tests/` 中相关用例
- 若改 `pa_agent/data/` 数据源或 `pa_agent/data/base.py` 的 `KlineBar` 契约，请先阅读 [README.md 的已知坑与修复记录](README.md#已知坑与修复记录重要)，特别是休市模式契约（坑 7、13）
- 若改 SSE 流（`/api/bars/stream`、`/api/analyze/stream`），请验证前端 `updateSSEStatusWithExpiry` 的 sanity check 仍生效（`remaining > tfSecs` 则丢弃）

## 问题反馈

- **Bug**：附上日志片段（`logs/pa_agent.log`）、复现步骤、品种/周期、浏览器 Console 截图（若是前端问题）
- **功能建议**：说明使用场景与期望行为
- **数据源问题**：注明交易所、品种代码、是否盘中 / 休市，附上 `/api/health/check` 的响应

请通过 [GitHub Issues](https://github.com/wujiaxiang/PA_Agent/issues) 提交。本仓库不提供原项目的 QQ 群交流渠道。

## 开发者参考

二次开发过程中踩过的坑与修复记录均汇总在 [README.md 的「已知坑与修复记录」](README.md#已知坑与修复记录重要)，后续 agent 调试时请先阅读，避免重复踩坑。核心约束：

- `latest_snapshot(n)` 契约：正常模式返回 `n+1` 根（1 forming + n closed），休市模式返回 `n` 根（全 closed）
- 消费方判断 forming bar 必须用 `has_forming_bar_at_head()`，不要硬编码 `bars[0].closed == False`
- SSE 后台循环用 `seconds_until_bar_closes` 决定推送间隔，休市时返回 0 触发 `_FALLBACK_WAIT_S` 兜底
- 切换交易所/品种需用 `Promise.allSettled` 并行执行，配合 `_inflightSwitch` 防重入
