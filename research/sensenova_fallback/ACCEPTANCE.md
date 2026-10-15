# SenseNova 免费模型兜底：最终验收

日期：2026-10-10。以本文件为最终结论；agent_result.md / agent_review_result.md 是各轮子代理的历史报告，后续独立审查已进一步修正。

## 已完成

只当 base_url 规范化为 `https://token.sensenova.cn/v1` 时启用。阶段一、阶段二的模型调用普通异常（HTTP/429/网络/超时等）、空响应、JSON/结构校验失败，以及校验重试中抛出的异常，进入免费池兜底。主模型成功则不调用免费池；其他地址保留旧行为。数据不足、有效 wait/reject、用户取消不视为应绕过的失败。

免费池来自 [dsh-our-free-model 的 Kilo 通道](https://github.com/Ebony-Vinyl/dsh-our-free-model/blob/main/src/kilo.js)：`https://api.kilo.ai/api/gateway`，实时筛选 `isFree=true`，不使用账号、密钥、OpenCode 伪装、EAC 或付费模型。未安装插件，未增加依赖，未更改 DSH 配置。

## 真实验收（不是仅问答连通）

使用生产 `PromptAssembler + JsonValidator + TwoStageOrchestrator + PendingWriter`，合成60根趋势K线；仅注入主接口连接异常，免费上游HTTP请求及模型生成均为真实。

- 第一次免费候选 `inclusionai/ling-3.1-flash` 实际返回 HTTP 429。
- 自动轮换 `nvidia/nemotron-3.5-lightning:free`，阶段一 HTTP 200、原有校验通过。
- 阶段二主接口再次注入失败；跳过冷却候选，Nemotron 再次 HTTP 200、原有校验通过。
- 事件：Stage1Started → Stage1Done → Stage2Started → Stage2Done → RecordSaved。
- `exception=null`，两个阶段均真正调用模型，耗时 **93.27秒**。
- 阶段一提示词107194字符，阶段二215501字符。不是缩短版模板回显；未改提示词或交易校验器。
- 完整提示词、返回、校验结果与记录：`independent_review/full-pipeline-20261010-152835/record.json`；日志 `run.log`；结果 `summary.json`。
- 另一次实测正常产生 wait，程序按原有安全闸门短路第二阶段并保存，证明没有为追求“成功”强制交易：`independent_review/full-pipeline-20261010-152651/`。

### 交付命令复验

从项目外目录 `D:\tools\data` 使用项目 `.venv` 运行 `verify_full_pipeline.py --trend`，再次完成两个阶段真实调用及保存，**103.48秒**。本次阶段一 Ling 成功；阶段二 Ling 真实429后自动切换 Nemotron 成功。证据：`runs/full-20261010-153201-5c6d766b/`。因此已分别观察到阶段一与阶段二免费候选限流后的真实轮换。

### 冻结代码后的最终复验

交付命令 `verify_free_pipeline.py` 与 `verify_free_fallback.py` 均从项目外目录用项目 `.venv` 运行成功。

- 完整分析（`runs/full-20261010-170049-d51b3d5e/`）：**95.73秒**，阶段一 Ling 成功，阶段二 Ling 真实429后自动切换 Nemotron 成功，两阶段均真实调用、校验与保存通过。
- 轻量连通（`runs/20261010T090033Z_verify.json`）：`ok=true`、`smoke_only=true`、`analysis_success=false`（连通性只标 smoke，不冒充分析成功）。

## 兜底期间的界面提示（2026-10-10 追加）

用户反馈"兜底时界面像卡死"。现已在兜底全程通过原有内容回调（Qt 信号，线程安全）输出进度行，写在「实时」页的撰写回答框里，全程不需要额外线程：

- 进入备用处理：`[提示] 阶段一的主通道暂时不可用，已自动切换备用通道继续分析。` + `[提示] 界面会显示当前处理进度，请稍候。`
- 每次尝试：`[提示] 正在等待备用通道返回（第 n/6 次尝试）…`
- 尝试失败：`[提示] 当前尝试未成功（用时 n 秒），正在继续处理…`（不显示限流/错误原因）
- 收到返回：`[提示] 已收到返回，正在校验结果…`
- 校验通过：`[提示] 校验通过（用时 n 秒），以下是本次分析结果：`（**在答案之前**输出，顺序有测试锁定）
- 校验失败：`[提示] 当前结果未通过检查（用时 n 秒），正在继续处理…`
- 全部失败：`[提示] 备用通道 n 次尝试均未成功，本次分析将按原错误结束。`

### 界面措辞规则（用户要求）

用户明确要求**界面不得出现免费模型/上游模型名**（原话「不要显示什么免费模型，这样就显得很廉价」）。因此：

- 用户可见文案统一用 `[提示] … 备用通道 …` 的中性措辞，**不出现** 模型 ID、provider 名、`api.kilo.ai`、以及"免费""兜底"等字样；
- 不在界面显示底层失败原因或单次等待上限；只提示"当前尝试未成功，正在继续处理"，避免把瞬时限流等内部细节暴露给用户；
- 真实来源仍完整保留在**日志**（`SenseNova 主接口失败，已由免费模型接管: provider=… model=…`）与**记录 JSON**（`fallback_provider` / `fallback_model` / `fallback_attempts`）中，只是不显示在界面上；
- 由 `test_progress_lines_never_advertise_the_free_lane` 等测试锁定，防止以后又把模型名写回界面。

真实运行证据（`runs/full-20261010-181921-cf791e34/`）：一次运行输出 **10 段提示、903 字节**，全程 156.7 秒，两个阶段均真实调用并通过校验；另一次 `runs/full-20261010-181255-311ab511/` 输出 13 段，其中包含某次返回未通过校验后自动换下一个候选。提示只在界面上显示，不写入记录、不参与校验、不改变失败关闭语义。

## 自动测试

- 新增兜底及独立边界测试：**38 passed**（`test_sensenova_fallback.py` 27 + `test_sensenova_fallback_review.py` 11）。
- 在项目 `.venv` 中运行兜底、校验重试、QClaw与核心两阶段回归：**46 passed**。
- 更广相关集合：**75 passed，3 failed**；3项均是修改前已经复现的KKAI兼容断言，并非新增回归。完整输出：`independent_review/final-regression.log`，修改前基线 `baseline.md`。
- `git diff --check` 通过。保留原有 market_features、prompt_engineering、pa_validation 等未提交改动。

## 最终独立修正

1. SDK 快速失败使用精确地址门控，不能用 `sensenova.cn` 子串匹配影响其他主机/路径。
2. 自查发现并已还原一处副作用的改动：曾让 SenseNova 跳过“服务端不接受 stream_options 时去掉该参数重试”的降级路径。429 立即失败已由 `max_retries=0` 保证，不需要扩大改动；现已加测试锁定该降级路径仍可用。
3. 仅对实际验证支持关闭思考的 Ling3.1 / Nemotron3.5-lightning 传 `reasoning.enabled=false`，避免长思考超时；其他模型不强制关闭。
4. 删除首轮多余的 OpenCode 通道及客户端指纹；仅使用公开免密 Kilo。

## 使用与边界

重启 PA_Agent 即可加载代码，原 SenseNova 配置不需要改。每轮仍先尝试主接口，不会把设置永久改成免费源。结果 raw 中有 `fallback_provider`、`fallback_model`、`fallback_attempts`。

每阶段最多尝试6个候选；模型清单缓存30分钟；429冷却5分钟，5xx冷却1分钟。单个免费请求HTTP超时120秒。所有候选失败则保留错误，不伪造分析结果、不跳过交易安全检查。

这是可用性兜底，不保证免费上游永远可用，也不保证不同模型的交易判断质量相同。若本机完全断网，免费源也无法救援。Kilo 免费池可能记录提示词；发送同一份分析内容但绝不携带 SenseNova 密钥。

## 代码与复测

- `pa_agent/ai/sensenova_fallback.py`
- `pa_agent/ai/deepseek_client.py`
- `pa_agent/orchestrator/two_stage.py`
- `tests/unit/test_sensenova_fallback.py`
- `tests/unit/test_sensenova_fallback_review.py`

轻量连通复测（不是完整分析验收）：

```text
D:\cl\PA_Agent\.venv\Scripts\python.exe D:\cl\PA_Agent\research\sensenova_fallback\verify_free_fallback.py
```

完整分析复测：

```text
D:\cl\PA_Agent\.venv\Scripts\python.exe D:\cl\PA_Agent\research\sensenova_fallback\verify_full_pipeline.py --trend
```

每次复测单独保存日志和记录。不启动GUI、不运行实盘、不下单；只读提示词并将结果写入测试目录。