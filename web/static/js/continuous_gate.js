/* 持续分析 / 等待收盘 的纯状态逻辑（无 DOM 依赖，可在 Node 中单测）。
 *
 * 抽出来有两个原因：
 *  1. 「刚收盘 bar 的 ts_open」计算原本在 app.js 里重复了 3 份
 *     （SSE bar_close handler、SSE 倒计时归零、轮询倒计时归零），
 *     三份必须永远保持一致，抽成唯一实现避免漂移。
 *  2. 「本次触发是否需要再等收盘」是持续分析能否工作的关键判定，
 *     之前内联在 startAnalysis/startIncrementalAnalysis 里，
 *     写错会导致持续分析整体失效，且无法单测。
 *
 * 契约：
 *   closedBarTs(bars) → 最新一根「已收盘」bar 的 ts_open（0 表示无）。
 *     按 bars[i].closed 标志倒序查找，**不依赖位置假设**：休市模式下全部
 *     bar 都已收盘，「刚收盘」就是最后一根；正常模式末位是 forming bar，
 *     往前一根才是刚收盘的。无 closed 标志时退回 offset 启发式。
 *
 *   shouldWaitForClose(triggerSource, waitCloseChecked)
 *     triggerSource='continuous' 时**永不等待**：持续分析本身就是被 bar_close
 *     事件触发的，此时 bar 刚刚收盘，再等一根会导致分析整整晚一个周期触发，
 *     且下一个 bar_close 会 stopWaitCloseCountdown() → resolve(false) 把它取消，
 *     表现为持续分析反复重置、永不真正发起分析。
 *     triggerSource='user'（点击分析/增量按钮）时才遵循复选框。
 */
(function (global) {
  'use strict';

  /** ascending-sorted 下「刚收盘 bar」相对末尾的偏移：-1 是 forming，-2 是刚收盘 */
  var CLOSED_BAR_OFFSET = 2;

  function closedBarTs(bars) {
    if (!Array.isArray(bars) || bars.length === 0) return 0;
    var sorted = bars.slice().sort(function (a, b) {
      return (a.ts_open || 0) - (b.ts_open || 0);
    });
    // 必须按 closed 标志找，**不能**假定「末位恒为 forming bar」。
    // 休市模式下全部 bar 都已收盘（AGENTS.md 数据快照契约：bars[0].seq=1,
    // closed=True），此时「刚收盘」就是最后一根；沿用 offset=2 会返回
    // 两根之前的 ts，导致 bar_close 哨兵错位 —— 重新开盘后可能误判
    // 「这根已处理过」而漏触发，或对同一根重复触发。
    for (var i = sorted.length - 1; i >= 0; i--) {
      if (sorted[i] && sorted[i].closed === true) {
        var ts = sorted[i].ts_open;
        if (typeof ts === 'number' && ts > 0) return ts;
      }
    }
    // 没有 closed 标志（老格式 / SSE 增量）：退回偏移启发式
    var idx = sorted.length >= CLOSED_BAR_OFFSET
      ? sorted.length - CLOSED_BAR_OFFSET
      : sorted.length - 1;
    var fallback = sorted[idx] && sorted[idx].ts_open;
    return typeof fallback === 'number' && fallback > 0 ? fallback : 0;
  }

  function shouldWaitForClose(triggerSource, waitCloseChecked) {
    if (triggerSource === 'continuous') return false;
    return !!waitCloseChecked;
  }

  global.PAContinuousGate = {
    CLOSED_BAR_OFFSET: CLOSED_BAR_OFFSET,
    closedBarTs: closedBarTs,
    shouldWaitForClose: shouldWaitForClose,
  };
})(typeof window !== 'undefined' ? window : globalThis);