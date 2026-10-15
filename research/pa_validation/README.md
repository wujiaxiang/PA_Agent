# PA 提示词实证验证 harness（v2）

验证 `prompt_engineering/` 中的概率说法是否被真实 K 线支持。
数据：`D:\MT5_K线数据` 的 XAUUSD / US100.cash / US500.cash（M5/M15/H1/H4/D1）。
特征定义复用项目源码（`pa_agent/ai/kline_features.py`、`market_features.py`、`indicators/`）。
结论见 `../PA_提示词工程实证验证报告.md`。

## 以 v2 为准

| 脚本 | 内容 |
|---|---|
| `common2.py` | 打乱 bar 的 NULL 基线、多空镜像、累计器 |
| `test1_v2_range.py` | 区间突破：三级区间定义、多种失败/成功口径 |
| `test2_v2_spike_null.py` | 尖峰余波，REAL vs NULL |
| `test3_v2_micro.py` | 微通道第一次破位，含普通强势段对照 |
| `test4_v2_hl.py` | 规范 H1/H2/H3 计数，必须成交，R 期望值 |
| `test5_v2_bars.py` | 趋势棒跟随与连串，全样本/非趋势棒基线 |
| `test6_v2_depth.py` | 回撤深度分档，REAL vs NULL vs 解析随机游走 |
| `test7_v2_20gb.py` | 20GB 首触 EMA，按缺口长度分桶，必须成交 |
| `check_hlcount.py` | 用项目真实函数统计程序 `hl_count` 的分布 |

## v1 脚本已作废

`test1_breakout.py`、`test3_microchannel.py`、`test4_hlcount.py`、`test5_followthrough.py`、`test6_pullback_depth.py`、`test7_20gb.py`、`test7b_20gb_robust.py` 有定义错误（缺基线、模拟未要求成交、区间未检验边界等），其输出不应再引用。
`test2_spike.py` 的检测函数仍被 `test2_v2_spike_null.py` 复用。

## 运行

```
cd research/pa_validation
uv run python test1_v2_range.py
uv run python test3_v2_micro.py
uv run python test4_v2_hl.py
uv run python test5_v2_bars.py
uv run python test6_v2_depth.py
uv run python test7_v2_20gb.py
uv run python test2_v2_spike_null.py
uv run python check_hlcount.py
```

需要 `pyarrow` 读取 parquet（已装在项目 `.venv`，仅用于验证）。

## v3：描述性市场规律（不谈交易优势）

结论见 `../PA_市场规律总结.md`。每个统计量同时对比两个基线：IID（打乱 bar 顺序）和 VOL（随机镜像 bar 方向、保留波动聚集）。

| 脚本 | 内容 |
|---|---|
| `common3.py` | VOL 基线、`compare` / `table` 汇总（含 14 条序列一致性计数） |
| `reg1_bars.py` | 收益自相关、同向延续、波动聚集、窄棒 / 内包线之后的幅度 |
| `reg2_regime.py` | 效率比 ER 的状态持续性 |
| `reg3_legs.py` | zigzag 腿、回撤比、斐波那契峰、等腿峰、双顶底、回访、腿内反向小腿数 |
| `reg5_ema.py` | EMA20 站队游程、缺口棒游程、拉伸距离、回到 EMA 的概率 |
| `reg6_breakout.py` | N 根极值突破之后的回测、收回、延续 |
| `reg8_bartypes.py` | 棒型转移提升度 |
| `reg7_time.py` | 分时波动、日高低点位置、开盘方向（日内打乱基线） |
| `reg9_roundnum.py` | 摆动点与整数价的位置关系 |
| `reg_extra.py` | ii / iii 与连续窄棒之后的幅度、整数关口一致性 |

## v4：带前后半段验证的条件统计

结果见 `../PA_条件统计结果.md`。规则见 `PREREG.md`（运行前写定，运行后未改）。

| 脚本 | 内容 |
|---|---|
| `PREREG.md` | 5 个假设的定义、窗口、切分、对照、判定规则 |
| `cond_stats.py` | 正式检验（H1–H5）；`python cond_stats.py placebo` 为快速零效应校准 |
| `cond_placebo_more.py` | 更大样本的零效应校准（真实与对照都换成独立零模型） |
| `cond_h5_followup.py` | H5 的事后探索（不属于预注册判定）：用保留分时波动的 VOL 零模型复核 |

## v5：缺口 / FVG

结果见 `../PA_缺口检验结果.md`。先定义并验证检测器，再预注册，再校准，最后正式运行。三个假设均 NOT-FOUND。

| 文件 | 内容 |
|---|---|
| `GAP_DEFINITIONS.md` | G3 / G3T / G3S 的精确规格、边界与等号、被排除的概念及理由 |
| `GAP_PREREG.md` | H6a / H6b / H7 的定义、分层、对照、判定规则（运行前写定） |
| `gap_defs.py` | 检测器 `detect`、结果函数 `outcomes`、`load_file`、`tf_seconds` |
| `test_gap_defs.py` | 检测器 18 项测试（合成案例、朴素实现交叉、因果性、镜像、不变量） |
| `gap_stats.py` | 正式检验；`python gap_stats.py placebo` 为零效应校准 |
| `test_gap_stats.py` | 统计部分 7 项测试 |
| `gap_sanity.py` / `gap_handcheck.py` / `gap_plot.py` | 频率与对称性检查、手算核对、事件作图（需 matplotlib，仅开发用） |
| `_figs/` | 事件示例图 |

测试：`uv run python -m pytest -q -p no:cacheprovider test_gap_defs.py test_gap_stats.py`
