# 经验库读端切库方案（v2 · 已评审修订）

> ## ⚠️ 本文已被最终实现推翻 —— 保留作决策记录，**不要照它实施**
>
> 本方案的核心是「**SQLite 优先，查不到回落文件**」，且读端「顺手补写」回填。
> **这两点都已被否决并废弃**：
>
> | 本文写的 | 最终实现（见 AGENTS「经验库：库是唯一真源」） |
> |---|---|
> | 先查 SQLite，miss 再扫文件 | **完全只查库，没有文件回落** —— 回落本身即旧设计的产物：同一条经验两个真源，静默分叉时没人知道该信哪个 |
> | 读端「顺手补写」回填 | **读端严格只读**。回填会把读变成写、顶住 `max_workers=2` 分析池，且破坏「写入方唯一入口」 |
> | 分析记录读文件 `file_path` | 分析记录**也已只查库**（正文取 `payload_json`）；磁盘副本降级为只写不读的归档 |
> | 复盘回写进 entry 的 JSON 字段 | 复盘走**独立表** `experience_reviews`，可重跑留历史 |
>
> 否决者：用户（明确要求「数据都写入数据库」「不要用文件这种过时的设计」）。
> 落地的改动见 CHANGELOG 第 8/9/10 条。
>
> 本文仍有价值的部分：§0 的评审裁决（B1 读失败标志非线程局部，已证实并修复）、
> §7 的评审推翻记录，以及各阶段拆分与回归基线方法。

> 上游：`650fc8f feat(auth): 注册登录占位层`
> 依据：[SESSION_STORAGE_DESIGN.md](SESSION_STORAGE_DESIGN.md) §7「迁移策略：双轨制，零回归」
> v1 经两轮独立评审（可行性 / 回归风险），**结论：不能按 v1 顺序开工**。本版是修订后方案。
> v1 的错误见 §7「评审推翻记录」，保留以免后来人重蹈。

---

## 0. 评审裁决摘要

| 编号 | 结论 | 处理 |
|---|---|---|
| **B1** | `hub.read_failed` 是**进程级共享可变标志**（`_read_error` 是普通实例属性，非 `threading.local`），任一线程成功读一次就把它清掉。**v1 的降级契约在并发下是死代码** | 已实测复现 → 提为 **P-1** |
| **B2** | reader 抛异常会**连同已付费的 Stage 1 一起丢掉整次分析**（`two_stage.py:678` 无 try/except，下一次落盘在 703） | 已实测确认 → 提为 **P-1** |
| **B3** | `list_entries(status=...)` 是**单值**，v1 伪代码 `status IN (...)` 不可调用；调两次会静默改选中集（≤10 候选 vs 合并 top5） | → **P-2** 增 `statuses` |
| **B4** | v1 的 P1 伪代码绕过 `read_top5()`，会**静默废掉 12 个 Mock 测试点** | → DB 优先逻辑改为放进 `read_top5()` 内部 |
| **B5** | v1 称浏览端周期筛选是**多选** —— 实测是单选（`index.html:507` 无 `multiple`，`app.js:5297` 取 `.value` 标量） | **删除该 work item** |
| R1 | 读路径「顺手补写」会把读变成写，顶住 `max_workers=2` 的分析池（`busy_timeout=5000`），且破坏 AGENTS「写入方唯一入口」 | **放弃顺手补写**，交给 importer |
| R2 | `entry_id` 主键不含 `user_id` 且不在 UPDATE SET 里 → 跨用户静默覆写 | → **P-2** 按 user_id 分区 |
| R3 | 既有单测切读后会**静默失效**（只写 tmp_path、DB 空 → 走文件回落 → 照样绿） | → P1 验收必须含 DB 主路径用例 |
| R4 | 复盘只存 DB 会破坏「文件是权威副本」不变式 | → 复盘**回写进条目文件**，无需新表 |
| R5 | `max_chars_per_entry` 在 `read_for_stage2` 里是**死参数**；且 `le=4000` < 真实 payload 7032 字符 → **调参救不了**，P4 是强制项 | 记录进 P4 |
| R6 | `RecordMeta.user_id` 会让 `/api/records` 响应多一个 key | 登记为接口变更 |

**已复核为正确、可直接执行的部分**：schema 迁移（`db.migrate()` 每次启动无条件跑
`all_statements()`，不看 `SCHEMA_VERSION`，新表名能到达已迁移的生产库）；
`RecordMeta` 加带默认字段对旧记录安全（三条加载路径都先 pop `_partial_reason` 再 validate）；
`_write` 先落盘再镜像；`finalize()` 会重读 JSON（故 user_id 天然带得出）；
不上 PG 的论证。

---

## 1. 现状（2026-10-05 实测）

身份层**已落地**：`storage/auth.py`、`web/api/auth_ctx.py::current_user_id()`、`users.py`。

经验库**读端一行未动**，三条路径全在文件系统：

| 读路径 | 位置 |
|---|---|
| 提示词注入 | `experience_reader.py:112` `iterdir()`（唯一漏斗是 `read_top5()`，被 `read_for_stage2:159` 调用） |
| 浏览 API | `routes_data.py:428/435/463/482` |
| 复盘取档 | `routes_experience_review.py:78/85` |

**零调用者**：`current_user_id`（`web/` 下除自身外）、`experience_repo.list_entries`/
`get_entry`/`count_by_status`（除测试外）。`experience_entries` 表 0 行 —— 死索引。

### 拦路虎

**① 身份三分裂**：`bind_session` 写 `"default"`（`session_ctx.py:123`）、
repo 与 auth 回落写 `"admin"`（`db.py:40`）。`users` 表只有 `admin`。
不先统一，切读当天经验库全空且无报错。

**② 写入方拿不到 user_id**：`RecordMeta` 无 user_id；`spawn_post_order_followup`
（daemon 线程）与 `experience_scheduler`（调度器线程）都不在请求线程。
⇒ `save_pending()` 必须把 user_id 写进 JSON，否则后台结算只能回落 admin。

---

## 2. 方案

沿用 §7 的 **C 阶段切读**（SQLite 读，miss 回退文件）。**不做 D 阶段**，不做顺手补写。

### P-1 · 故障隔离前置（独立于切读，可单独 PR 验证）

**为什么必须先做**：v1 的降级安全网建立在 `read_failed` 上，而它不可靠 —— 不修就切读，
等于在并发下把「DB 抖动」放大成「经验库静默变空」，正是本方案要防的失败模式。

| 文件 | 改动 |
|---|---|
| `pa_agent/storage/db.py` | `_read_error` 从实例属性移入 `self._local`（与 `conn` 同域）。**这修的是全系统级隐患**，不只是经验库 |
| `pa_agent/storage/experience_repo.py` | 查询函数**显式回传失败标志**（返回 `(rows, failed)` 或等价）。调用方不再「查完再去读共享标志」，从根上消除读后竞态 |
| `pa_agent/orchestrator/two_stage.py` | ① reader 调用包 try/except，异常 → `experience_entries = []` + warning；② `save_partial` 前移到 `Stage1Done` 之后立即执行，使「已付费的 Stage 1」在 reader 异常时也不丢 |

**验收**：并发线程各自失败/成功互不干扰；reader 抛异常时 `save_partial` 已落盘且分析正常返回。

### P-2 · repo 接口补齐

| 文件 | 改动 |
|---|---|
| `pa_agent/storage/experience_repo.py` | ① `list_entries` 增 `statuses: list[str] \| None`（`status` 单值保留兼容）；② `_entry_id` 改为 **`f"{user_id}_{path.stem}"`** |
| — | ②的取舍：SQLite 无法 ALTER 主键，复合主键要重建表。**改 `entry_id` 取值**（只影响 DB 列，不动文件名）零迁移成本，且同用户状态流转仍是同一 id → 仍是 UPDATE，不会产生重复行 |

**验收**：`list_entries(statuses=["success","failure"], limit=5)` 返回的 entry_id 集合
== 文件路径合并取最新 5 条的 entry_id 集合；两个用户写同 stem → 两行且互不覆盖。

### P0 · 身份统一

| 文件 | 改动 |
|---|---|
| `web/api/session_ctx.py` | `bind_session` 形参默认 `""`，为空时调 `current_user_id(request)` |
| `pa_agent/records/schema.py` | `RecordMeta` 增 `user_id: str = ""`（旧记录读出为 `""`） |
| `pa_agent/records/experience_writer.py` | `save` / `save_pending` / `save_pending_if_resolvable` / `_write` / `_mirror_to_sqlite` 全链透传 `user_id`；`save_pending` 写进 JSON。**注意 `save()` 目前不镜像 SQLite**，一并补上避免将来复活时写出永远进不了库的条目 |
| `web/api/order_followup.py` | `spawn_post_order_followup(user_id=...)`，在**同步段**取 `current_user_id(request)` |
| `web/api/experience_verifier.py` | 结算用记录自身的 `content["user_id"]`，缺则回落 `DEFAULT_USER_ID` 并记 warning |

**接口变更**：`/api/records` 响应中 legacy 记录的 `meta.user_id` 为 `""`（多一个 key）。

### P1 · `ExperienceReader` 切读（C 阶段核心）

**关键设计：DB 优先逻辑放进 `read_top5()` 内部**，`read_for_stage2` 继续只调 `read_top5`。
这样 14 个 Mock 测试点（integration/e2e/unit）全部保持有效 —— 它们是现成的回归网，绕过它们
等于把 12 个测试变成「什么都没测且不会红」。

```
read_top5(cycle_position)
  ├─ list_entries(statuses=["success","failure"], cycle_position=…) 命中 → 直接用
  ├─ DB 为空 / 查询 failed（用 P-1 的显式标志，不用 read_failed）→ 回落现有 iterdir()
  └─ ✗ 不做「顺手补写」：读路径不写（见 §4 裁决）
```

- **打分逻辑留在 Python**（`_score` 168-181）：只有方向 +2、形态交集两项，数据量百级。
  SQL 化收益低、回归面大。
- **顺带修 `direction` 枚举错配**：writer 落盘的是阶段二 `order_direction`（校验限定
  `["做多","做空"]`，`json_validator.py:797`），reader 却拿它跟阶段一的
  `bullish/bearish/neutral`（`市场诊断框架.txt:1375`）比 → **+2 分恒为 0**。
- **顺带清理死参数** `read_for_stage2(max_chars_per_entry=…)`：收了从不使用，
  真正截断在 `prompt_assembler.py:1974`。
- **回落扫描加显式 guard**：保持 `EXPERIENCE_DIR/<cycle>` 定位（不换成
  `importer.iter_real_experience_files`）；加显式点号目录拒绝。当前安全性是**偶然**的
  （`.seed_demo_*` 恰是各 cycle 目录的兄弟），而 writer 侧 `_safe_segment` 会剥前导点、
  reader 侧不会，两边不对称。

**验收**：① DB 有行时不触碰文件系统（断言 `iterdir` 未被调用）；② DB disabled /
query failed 时回落文件，行为与改造前**逐字一致**；③ DB 路径与文件路径返回同一 entry_id 集合；
④ 新增 DB 主路径用例（现有两条只写 tmp_path 的用例切读后会静默失效）。

### P2 · 浏览 API 切读

`routes_data.py::_scan()` → `list_entries()` + `count_by_status()`。
`_read_status_dir()`（pending/unresolved）删除 —— DB 里有行，直接查。
`#exp-cycle` 是**单选**，repo 无需多 cycle 参数（v1 的该 work item 系事实错误，已删）。

### P3 · 复盘回写进条目文件（不新增只存 DB 的表）

复盘结果**经 `ExperienceWriter` 回写进 entry JSON 的 `review` 字段**，
不换目录、不改文件名（复盘不改变记录的状态与时序）。落盘后由既有 `_mirror_to_sqlite`
把 `content_json` 一并镜像，DB 侧无需新表、无需 schema 迁移。

这样一并解决：AGENTS「数据来源必须可追溯」、§7「文件是权威副本」、
以及 reviewer 指出的「只存 DB 不可重建」。

- 新增 `ExperienceWriter.attach_review(path, review)`，沿用 `_write` 的 `tmp + os.replace`
- `_find_entry()` → `get_entry(record_id, user_id=…)`，**user_id 必传**
- **落盘时机**：SSE 断连就没了，用户中途关页面等于白刷 ⇒ 由**后端**在 `done` 事件前落盘，
  不依赖前端回报

### P4 · 字段感知渲染（**强制项**，不是优化）

`_render_experience`（`prompt_assembler.py:1952-1977`）现在整块 `json.dumps(indent=2)`
后按 `max_chars_per_entry`（默认 400）**盲截**。实测真实 payload：

```
完整 payload: 7032 字符
analysis_context 起始偏移: 471     ← 默认 cap=400 完全不可见
bars_snapshot   起始偏移: 2049
cap=1000: analysis_ctx=Y  bars=N      cap=2000: 同      cap=4000: 都可见但截在 bars 中间
```

而 `experience_max_chars_per_entry` 上限是 `le=4000`（`settings.py:65`）——
**即便调到最大也装不下 7032 字符，调参不是捷径**。不改渲染，P3 写进文件的内容
永远进不了推理。

改为字段感知：先渲染检索摘要字段，再单独追加复盘结论短文本。

---

## 3. 落地顺序

```
P-1 故障隔离 ──┬─→ P-2 repo 接口 ──┬─→ P1 reader 切读 ──→ P2 浏览 API
               │                    └─→ P0 身份统一 ────→ P3 复盘回写 ──→ P4 字段感知渲染
```

P-1 必须最先（否则后续任何降级都不可信）。
P-1 与 P-2 相互独立，可并行；P1 依赖 P-1+P-2；P3 依赖 P0。

---

## 4. 关键裁决

- **放弃「顺手补写」**：读路径不写。理由三条 —— ① 会顶住 `max_workers=2` 的分析池
  （`busy_timeout=5000`，一次撞锁可占住一个分析槽 5 秒）；② 破坏 AGENTS「写入方唯一入口」；
  ③ reader 自声明 strictly read-only。回填交给 importer 定期跑（幂等，本就存在）。
- **DB 优先塞进 `read_top5()` 而非 `read_for_stage2`**：保住 14 个 Mock 测试点。
- **`entry_id` 改取值而非改主键**：SQLite 不能 ALTER 主键，复合主键要重建表；改取值零迁移。
- **复盘写进条目文件而非新表**：保住「文件是权威副本」与可追溯性，且免 schema 迁移。
- **不上 PostgreSQL**：`db.py:20` 是 `sqlite3`，`_ConnectionHub` 依赖 `ON CONFLICT` /
  `AUTOINCREMENT` / `threading.local`。
- **不做 D 阶段（SQLite only）**：风险高，本次收益零。

## 5. 已知既有缺陷（本次只登记，不修）

- **连接泄漏**：`_all_conns` 只在 `close_all()` 清；`spawn_post_order_followup` 每次分析
  新建线程（`order_followup.py:212`）→ 每次分析 +1 条永不关闭的 SQLite 连接。
  实测 20 个一次性线程 → 21 条连接。P1 让更多调用点碰 DB，**放大了泄漏面**。
  建议单独排期修（`finally` 里 `close()` 本线程连接）。
- **`save()` 不镜像 SQLite**：P0 一并补上。

## 6. 风险登记

| 风险 | 缓解 |
|---|---|
| DB 一坏经验库静默变空 | P-1 显式失败标志 + 文件回落；单测覆盖 disabled 与 query-failed 两条分支 |
| 切读后历史 pending 无 user_id | P0 起新记录必带；历史回落 `DEFAULT_USER_ID` 并记 warning |
| `two_stage` 是分析热路径 | reader 调用包 try/except，异常不外冒；`save_partial` 前移 |
| 既有单测静默失效 | P1 验收必须含 DB 主路径用例；Mock 点保持有效 |
| 跨用户静默覆写 | P2 按 user_id 分区 entry_id |
| 与并行会话冲突 | 开工前登记写入范围；提交用 `git commit -- <显式文件列表>` |

## 7. 评审推翻记录（v1 的错误，勿重蹈）

1. **降级契约建在不可靠的信号上** —— `read_failed` 非线程局部，且成功读会清标志。
   这是 v1 最严重的错误：它让整条「DB 坏了也安全」的论证失效。
2. **漏了 reader 异常会毁掉整次分析** —— v1 §6 写「异常不外冒」，但代码里没有任何地方实现它。
3. **伪代码不可调用**（`status IN` vs 单值 `status`）且会静默改选中集。
4. **绕过 `read_top5()`** 会静默废掉 12 个测试点。
5. **基于错误前提**（周期筛选多选）造了一个无用 work item。
6. 引用行号漂移（`db.py:36`→`:40`、`db.py:190`→`:201`）—— db.py 当天改过。
   声明本身成立，行号过期。
