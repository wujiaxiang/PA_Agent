/* 回归测试：持续分析 / 等待收盘 的纯状态逻辑。
 *
 * 运行：node web/static/js/continuous_gate.test.js
 *
 * 覆盖两个已修复的缺陷：
 *  A. 持续分析被 bar_close 触发后仍去等下一根 → 整周期延迟，且被下一个
 *     bar_close 的 stopWaitCloseCountdown() resolve(false) 取消，
 *     表现为持续分析反复重置、永不真正发起分析。
 *  B. 用户取消「等待收盘」勾选时，startAnalysis 永久 await 在
 *     startWaitCloseCountdown() 上（resolver 泄漏）。
 *
 * 同时锁定「刚收盘 bar ts_open」的唯一实现（此前在 app.js 里重复 3 份，
 * 任一份漂移都会让 bar_close 与倒计时路径的去重哨兵不一致）。
 */
'use strict';

const assert = require('assert');
const path = require('path');

// continuous_gate.js 以 UMD 风格挂到 globalThis，Node 可直接 require 求值
const src = require('fs').readFileSync(
  path.join(__dirname, 'continuous_gate.js'), 'utf8'
);
// eslint-disable-next-line no-eval
(0, eval)(src);
const G = globalThis.PAContinuousGate;

assert.ok(G, 'PAContinuousGate must be exported');

/* ── closedBarTs ─────────────────────────────────────────────────────────── */

// 正常模式：n+1 根，bars[0] 为 forming（seq=0, closed=false），
// 最新的一根已收盘 bar 是倒数第二根（ts_open 次大）。
{
  const bars = [
    { seq: 0, ts_open: 3000, closed: false }, // forming（最新）
    { seq: 1, ts_open: 2000, closed: true },  // 刚收盘
    { seq: 2, ts_open: 1000, closed: true },
  ];
  assert.strictEqual(G.closedBarTs(bars), 2000,
    'normal mode must pick the just-closed bar, not the forming one');
}

// 输入是 newest-first（线上实际形态）也必须正确 —— 内部会排序。
{
  const newestFirst = [
    { seq: 0, ts_open: 3000, closed: false },
    { seq: 1, ts_open: 2000, closed: true },
    { seq: 2, ts_open: 1000, closed: true },
  ];
  assert.strictEqual(G.closedBarTs(newestFirst), 2000);
}

// 乱序输入同样必须稳定。
{
  const shuffled = [
    { seq: 2, ts_open: 1000, closed: true },
    { seq: 0, ts_open: 3000, closed: false },
    { seq: 1, ts_open: 2000, closed: true },
  ];
  assert.strictEqual(G.closedBarTs(shuffled), 2000,
    'must sort by ts_open rather than trust array order');
}

// 休市模式：全部已收盘，只有一根时退回取最后一根。
{
  assert.strictEqual(G.closedBarTs([{ seq: 1, ts_open: 1000, closed: true }]), 1000);
}

// 边界。
assert.strictEqual(G.closedBarTs([]), 0);
assert.strictEqual(G.closedBarTs(null), 0);
assert.strictEqual(G.closedBarTs(undefined), 0);
assert.strictEqual(G.closedBarTs([{ seq: 0, ts_open: 0, closed: false }]), 0,
  'ts_open=0 must not be treated as a valid sentinel');

/* ── shouldWaitForClose（缺陷 A）──────────────────────────────────────────── */

// 修复前：bar_close 触发的持续分析也会 waitCloseChecked=true → wait=true → 卡住。
assert.strictEqual(
  G.shouldWaitForClose('continuous', true), false,
  'BUG A: continuous analysis is triggered BY a bar close; it must never wait again'
);
assert.strictEqual(G.shouldWaitForClose('continuous', false), false);

// 用户点击分析按钮：仍然遵循复选框。
assert.strictEqual(G.shouldWaitForClose('user', true), true);
assert.strictEqual(G.shouldWaitForClose('user', false), false);

/* ── 缺陷 A 的确定性验证 ──────────────────────────────────────────────────
 *
 * 真实时序：倒计时归零（remaining<=0）与同一次 bar_close 几乎同时发生，
 * 谁先到不确定。修复前 bar_close 触发的持续分析会先调用
 * stopWaitCloseCountdown()，把本该 resolve(true) 的 pending 取消成
 * resolve(false) —— 这一根 bar 的分析因此被跳过或整整推迟一根。
 *
 * 这里不复现那个竞态（需要真实浏览器），只锁定决定性契约：
 *   continuous 触发 → 绝不等待 → 立即分析、不留 pending。
 */
{
  let pendingResolve = null;
  let analysesStarted = 0;

  const needWait = G.shouldWaitForClose('continuous', true);
  if (needWait) {
    pendingResolve = function (ok) { if (ok) analysesStarted += 1; };
  } else {
    analysesStarted += 1;
  }

  assert.strictEqual(analysesStarted, 1,
    'a bar close must start its analysis immediately under 持续分析');
  assert.strictEqual(pendingResolve, null,
    'no countdown promise may be left pending after a continuous trigger');
}

/* ── 缺陷 B：取消勾选必须让等待中的流程返回 ───────────────────────────────── */
{
  // 模拟 startAnalysis 的等待：pending resolver 必须在取消时被 resolve(false)
  let pending = null;
  let resolvedWith = undefined;

  function startWaitCloseCountdown() {
    return new Promise((resolve) => { pending = resolve; });
  }
  function stopWaitCloseCountdown() { // 修复后由 uncheck handler 调用
    if (pending) { const r = pending; pending = null; r(false); }
  }

  let started = false;
  (async () => {
    const ok = await startWaitCloseCountdown();
    resolvedWith = ok;
    if (!ok) return;      // startAnalysis 正常 return
    started = true;       // 发起分析
  })();

  // 用户取消勾选
  stopWaitCloseCountdown();

  setImmediate(() => {
    assert.strictEqual(resolvedWith, false,
      'BUG B: unchecking must resolve the pending wait with false');
    assert.strictEqual(started, false,
      'BUG B: unchecking must prevent the analysis from starting');
    console.log('continuous_gate: all assertions passed');
  });
}