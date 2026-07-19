// indicators.js — 自建指标库（计算 + 管理 + 图表集成 + 模态 UI）
//
// 重要说明：
//   TradingView 完整指标库（100+ 指标 + UI）属于商业版 Charting Library，
//   需单独申请授权（免费但限制场景），无法直接集成。
//   lightweight-charts（开源版）只提供渲染原语，无内置指标。
//   本文件实现常用指标的计算逻辑 + 自定义管理 UI。
//
// 支持的指标：
//   - 主图叠加 (overlay): SMA / EMA / BOLL / VWAP / SAR
//   - 副图独立 (oscillator): RSI / MACD / KDJ / ATR / OBV / WR
//
// 依赖：lightweight-charts v4.2（window.LightweightCharts），由 chart.js 创建主图
// 全局状态：window._indicatorsState（管理已启用指标 + 对应 series 引用）

// ── 指标注册表 ────────────────────────────────────────────────────────
// type: 'overlay' 主图叠加 / 'oscillator' 副图独立
// category: 'trend' 趋势 / 'momentum' 动量 / 'volatility' 波动 / 'volume' 量能
// params: 可配置参数列表
// outputs: 输出线列表（color/lineWidth/isHistogram）
// levels: 副图水平参考线（如 RSI 的 30/70）
const INDICATOR_REGISTRY = {
  // ── 主图叠加 ────────────────────────────────────────────
  sma: {
    name: 'SMA 简单移动平均', type: 'overlay', category: 'trend',
    params: [{ key: 'period', label: '周期', default: 20, min: 1, max: 500, step: 1 }],
    outputs: [{ key: 'sma', label: 'SMA', color: '#ffc800', lineWidth: 1 }],
  },
  ema: {
    name: 'EMA 指数移动平均', type: 'overlay', category: 'trend',
    params: [{ key: 'period', label: '周期', default: 20, min: 1, max: 500, step: 1 }],
    outputs: [{ key: 'ema', label: 'EMA', color: '#ffc800', lineWidth: 1 }],
  },
  boll: {
    name: 'BOLL 布林带', type: 'overlay', category: 'volatility',
    params: [
      { key: 'period', label: '周期', default: 20, min: 2, max: 500, step: 1 },
      { key: 'stddev', label: '标准差倍数', default: 2, min: 0.1, max: 10, step: 0.1 },
    ],
    outputs: [
      { key: 'upper', label: '上轨', color: '#ef5350', lineWidth: 1 },
      { key: 'middle', label: '中轨', color: '#ffc800', lineWidth: 1 },
      { key: 'lower', label: '下轨', color: '#26a69a', lineWidth: 1 },
    ],
  },
  vwap: {
    name: 'VWAP 成交量加权均价', type: 'overlay', category: 'volume',
    params: [],
    outputs: [{ key: 'vwap', label: 'VWAP', color: '#29b6f6', lineWidth: 2 }],
  },
  sar: {
    name: 'SAR 抛物线指标', type: 'overlay', category: 'trend',
    params: [
      { key: 'af_step', label: '加速因子步长', default: 0.02, min: 0.001, max: 0.5, step: 0.001 },
      { key: 'af_max', label: '加速因子上限', default: 0.2, min: 0.01, max: 1, step: 0.01 },
    ],
    outputs: [{ key: 'sar', label: 'SAR', color: '#ff9800', lineWidth: 1, point: true }],
  },

  // ── 副图独立 ────────────────────────────────────────────
  rsi: {
    name: 'RSI 相对强弱指数', type: 'oscillator', category: 'momentum',
    params: [{ key: 'period', label: '周期', default: 14, min: 2, max: 500, step: 1 }],
    outputs: [{ key: 'rsi', label: 'RSI', color: '#ffc800', lineWidth: 1 }],
    levels: [{ value: 30, color: '#26a69a' }, { value: 50, color: '#787b86' }, { value: 70, color: '#ef5350' }],
    height: 120,
  },
  macd: {
    name: 'MACD 指数平滑异同', type: 'oscillator', category: 'momentum',
    params: [
      { key: 'fast', label: '快线周期', default: 12, min: 2, max: 500, step: 1 },
      { key: 'slow', label: '慢线周期', default: 26, min: 2, max: 500, step: 1 },
      { key: 'signal', label: '信号周期', default: 9, min: 1, max: 500, step: 1 },
    ],
    outputs: [
      { key: 'macd', label: 'MACD', color: '#29b6f6', lineWidth: 1 },
      { key: 'signal', label: 'Signal', color: '#ff9800', lineWidth: 1 },
      { key: 'hist', label: 'Hist', color: '#26a69a', lineWidth: 1, isHistogram: true },
    ],
    levels: [{ value: 0, color: '#787b86' }],
    height: 140,
  },
  kdj: {
    name: 'KDJ 随机指标', type: 'oscillator', category: 'momentum',
    params: [
      { key: 'k_period', label: 'K 周期', default: 9, min: 1, max: 500, step: 1 },
      { key: 'k_smooth', label: 'K 平滑', default: 3, min: 1, max: 50, step: 1 },
      { key: 'd_smooth', label: 'D 平滑', default: 3, min: 1, max: 50, step: 1 },
    ],
    outputs: [
      { key: 'k', label: 'K', color: '#29b6f6', lineWidth: 1 },
      { key: 'd', label: 'D', color: '#ff9800', lineWidth: 1 },
      { key: 'j', label: 'J', color: '#ef5350', lineWidth: 1 },
    ],
    levels: [{ value: 20, color: '#26a69a' }, { value: 50, color: '#787b86' }, { value: 80, color: '#ef5350' }],
    height: 140,
  },
  atr: {
    name: 'ATR 平均真实波幅', type: 'oscillator', category: 'volatility',
    params: [{ key: 'period', label: '周期', default: 14, min: 1, max: 500, step: 1 }],
    outputs: [{ key: 'atr', label: 'ATR', color: '#ffc800', lineWidth: 1 }],
    height: 100,
  },
  obv: {
    name: 'OBV 能量潮', type: 'oscillator', category: 'volume',
    params: [],
    outputs: [{ key: 'obv', label: 'OBV', color: '#29b6f6', lineWidth: 1 }],
    height: 100,
  },
  wr: {
    name: 'WR 威廉指标', type: 'oscillator', category: 'momentum',
    params: [{ key: 'period', label: '周期', default: 14, min: 1, max: 500, step: 1 }],
    outputs: [{ key: 'wr', label: 'WR', color: '#ffc800', lineWidth: 1 }],
    levels: [{ value: -20, color: '#ef5350' }, { value: -50, color: '#787b86' }, { value: -80, color: '#26a69a' }],
    height: 120,
  },
};

// ── 全局状态 ──────────────────────────────────────────────────────────
// activeIndicators: 数组，每项 = { id, key, params, visible, _series, _subChart }
//   id: 实例 ID（同一种指标可加多个，如 EMA10 + EMA20）
//   key: registry key（'ema'/'rsi'/...）
//   params: { period: 20, ... }
//   visible: 是否显示
//   _series: 主图模式 → { outputKey: LineSeries }, 副图模式 → 同上
//   _subChart: 副图模式 → 独立 chart 实例
window._indicatorsState = window._indicatorsState || {
  activeIndicators: [],
  nextId: 1,
  oscContainer: null,
  mainChart: null,
  mainCandleSeries: null,
};

function _getCtx() {
  return {
    chart: window._indicatorsState.mainChart,
    candleSeries: window._indicatorsState.mainCandleSeries,
    oscContainer: window._indicatorsState.oscContainer,
  };
}

// ── 计算函数 ──────────────────────────────────────────────────────────
function _sortBars(bars) {
  return [...(bars || [])].sort((a, b) => a.ts_open - b.ts_open);
}

function _toTimes(bars) {
  return bars.map(b => b.ts_open / 1000);
}

function calcSMA(bars, period) {
  const out = [];
  if (!bars || bars.length < period) return out;
  let sum = 0;
  for (let i = 0; i < bars.length; i++) {
    sum += bars[i].close;
    if (i >= period) sum -= bars[i - period].close;
    if (i >= period - 1) out.push({ time: bars[i].ts_open / 1000, value: sum / period });
  }
  return out;
}

function calcEMA(bars, period) {
  const out = [];
  if (!bars || bars.length < period) return out;
  const k = 2 / (period + 1);
  let ema = 0;
  for (let i = 0; i < period; i++) ema += bars[i].close;
  ema /= period;
  out.push({ time: bars[period - 1].ts_open / 1000, value: ema });
  for (let i = period; i < bars.length; i++) {
    ema = bars[i].close * k + ema * (1 - k);
    out.push({ time: bars[i].ts_open / 1000, value: ema });
  }
  return out;
}

function _calcEMAFromCloses(closes, period, times) {
  const out = [];
  if (closes.length < period) return out;
  const k = 2 / (period + 1);
  let ema = 0;
  for (let i = 0; i < period; i++) ema += closes[i];
  ema /= period;
  out.push({ time: times[period - 1], value: ema });
  for (let i = period; i < closes.length; i++) {
    ema = closes[i] * k + ema * (1 - k);
    out.push({ time: times[i], value: ema });
  }
  return out;
}

function calcBOLL(bars, period, stddev) {
  if (!bars || bars.length < period) return { upper: [], middle: [], lower: [] };
  const middle = calcSMA(bars, period);
  const upper = [], lower = [];
  for (let i = period - 1; i < bars.length; i++) {
    const mean = middle[i - period + 1].value;
    let sumSq = 0;
    for (let j = i - period + 1; j <= i; j++) {
      sumSq += Math.pow(bars[j].close - mean, 2);
    }
    const sd = Math.sqrt(sumSq / period);
    upper.push({ time: bars[i].ts_open / 1000, value: mean + stddev * sd });
    lower.push({ time: bars[i].ts_open / 1000, value: mean - stddev * sd });
  }
  return { upper, middle, lower };
}

function calcVWAP(bars) {
  if (!bars || !bars.length) return [];
  const out = [];
  let cumPV = 0, cumVol = 0;
  for (const b of bars) {
    const tp = (b.high + b.low + b.close) / 3;
    const vol = b.volume || 0;
    if (vol > 0) { cumPV += tp * vol; cumVol += vol; }
    out.push({ time: b.ts_open / 1000, value: cumVol > 0 ? cumPV / cumVol : b.close });
  }
  return out;
}

function calcRSI(bars, period) {
  if (!bars || bars.length < period + 1) return [];
  const out = [];
  let gain = 0, loss = 0;
  for (let i = 1; i <= period; i++) {
    const ch = bars[i].close - bars[i - 1].close;
    if (ch > 0) gain += ch; else loss -= ch;
  }
  let avgG = gain / period, avgL = loss / period;
  out.push({ time: bars[period].ts_open / 1000, value: avgL === 0 ? 100 : 100 - 100 / (1 + avgG / avgL) });
  for (let i = period + 1; i < bars.length; i++) {
    const ch = bars[i].close - bars[i - 1].close;
    avgG = (avgG * (period - 1) + (ch > 0 ? ch : 0)) / period;
    avgL = (avgL * (period - 1) + (ch < 0 ? -ch : 0)) / period;
    out.push({ time: bars[i].ts_open / 1000, value: avgL === 0 ? 100 : 100 - 100 / (1 + avgG / avgL) });
  }
  return out;
}

function calcMACD(bars, fast, slow, signal) {
  const closes = bars.map(b => b.close);
  const times = _toTimes(bars);
  const emaFast = _calcEMAFromCloses(closes, fast, times);
  const emaSlow = _calcEMAFromCloses(closes, slow, times);
  const offset = slow - fast;
  const macdLine = [];
  for (let i = 0; i < emaSlow.length; i++) {
    macdLine.push({ time: emaSlow[i].time, value: emaFast[i + offset].value - emaSlow[i].value });
  }
  const macdCloses = macdLine.map(d => d.value);
  const macdTimes = macdLine.map(d => d.time);
  const signalLine = _calcEMAFromCloses(macdCloses, signal, macdTimes);
  const sigOffset = macdLine.length - signalLine.length;
  const hist = [];
  for (let i = 0; i < signalLine.length; i++) {
    hist.push({ time: signalLine[i].time, value: macdLine[i + sigOffset].value - signalLine[i].value });
  }
  return { macd: macdLine, signal: signalLine, hist };
}

function calcKDJ(bars, kPeriod, kSmooth, dSmooth) {
  if (bars.length < kPeriod) return { k: [], d: [], j: [] };
  const rsvs = [];
  for (let i = kPeriod - 1; i < bars.length; i++) {
    let hh = -Infinity, ll = Infinity;
    for (let j = i - kPeriod + 1; j <= i; j++) {
      if (bars[j].high > hh) hh = bars[j].high;
      if (bars[j].low < ll) ll = bars[j].low;
    }
    rsvs.push({ time: bars[i].ts_open / 1000, value: hh === ll ? 50 : (bars[i].close - ll) / (hh - ll) * 100 });
  }
  const k = [];
  for (let i = 0; i < rsvs.length; i++) {
    const start = Math.max(0, i - kSmooth + 1);
    let sum = 0;
    for (let j = start; j <= i; j++) sum += rsvs[j].value;
    k.push({ time: rsvs[i].time, value: sum / (i - start + 1) });
  }
  const d = [];
  for (let i = 0; i < k.length; i++) {
    const start = Math.max(0, i - dSmooth + 1);
    let sum = 0;
    for (let j = start; j <= i; j++) sum += k[j].value;
    d.push({ time: k[i].time, value: sum / (i - start + 1) });
  }
  const j = k.map((kv, i) => ({ time: kv.time, value: 3 * kv.value - 2 * (d[i] ? d[i].value : kv.value) }));
  return { k, d, j };
}

function calcATR(bars, period) {
  if (bars.length < period + 1) return [];
  const trs = [];
  for (let i = 1; i < bars.length; i++) {
    const h = bars[i].high, l = bars[i].low, pc = bars[i - 1].close;
    trs.push(Math.max(h - l, Math.abs(h - pc), Math.abs(l - pc)));
  }
  const out = [];
  let atr = 0;
  for (let i = 0; i < period; i++) atr += trs[i];
  atr /= period;
  out.push({ time: bars[period].ts_open / 1000, value: atr });
  for (let i = period; i < trs.length; i++) {
    atr = (atr * (period - 1) + trs[i]) / period;
    out.push({ time: bars[i + 1].ts_open / 1000, value: atr });
  }
  return out;
}

function calcOBV(bars) {
  if (!bars.length) return [];
  const out = [{ time: bars[0].ts_open / 1000, value: 0 }];
  for (let i = 1; i < bars.length; i++) {
    const prev = out[out.length - 1];
    const dir = bars[i].close > bars[i - 1].close ? 1 : bars[i].close < bars[i - 1].close ? -1 : 0;
    out.push({ time: bars[i].ts_open / 1000, value: prev.value + dir * (bars[i].volume || 0) });
  }
  return out;
}

function calcWR(bars, period) {
  if (bars.length < period) return [];
  const out = [];
  for (let i = period - 1; i < bars.length; i++) {
    let hh = -Infinity, ll = Infinity;
    for (let j = i - period + 1; j <= i; j++) {
      if (bars[j].high > hh) hh = bars[j].high;
      if (bars[j].low < ll) ll = bars[j].low;
    }
    out.push({ time: bars[i].ts_open / 1000, value: hh === ll ? -50 : (hh - bars[i].close) / (hh - ll) * -100 });
  }
  return out;
}

function calcSAR(bars, afStep, afMax) {
  if (bars.length < 2) return [];
  const out = [];
  let bull = bars[1].close > bars[0].close;
  let ep = bull ? bars[0].high : bars[0].low;
  let sar = bull ? bars[0].low : bars[0].high;
  let af = afStep;
  out.push({ time: bars[0].ts_open / 1000, value: sar });
  for (let i = 1; i < bars.length; i++) {
    sar = sar + af * (ep - sar);
    if (bull) {
      if (bars[i].low < sar) {
        bull = false; sar = ep; ep = bars[i].low; af = afStep;
      } else if (bars[i].high > ep) {
        ep = bars[i].high; af = Math.min(af + afStep, afMax);
      }
    } else {
      if (bars[i].high > sar) {
        bull = true; sar = ep; ep = bars[i].high; af = afStep;
      } else if (bars[i].low < ep) {
        ep = bars[i].low; af = Math.min(af + afStep, afMax);
      }
    }
    out.push({ time: bars[i].ts_open / 1000, value: sar });
  }
  return out;
}

// 统一计算入口
function calculateIndicator(key, bars, params) {
  const arr = _sortBars(bars);
  switch (key) {
    case 'sma':  return { sma: calcSMA(arr, params.period) };
    case 'ema':  return { ema: calcEMA(arr, params.period) };
    case 'boll': return calcBOLL(arr, params.period, params.stddev);
    case 'vwap': return { vwap: calcVWAP(arr) };
    case 'sar':  return { sar: calcSAR(arr, params.af_step, params.af_max) };
    case 'rsi':  return { rsi: calcRSI(arr, params.period) };
    case 'macd': return calcMACD(arr, params.fast, params.slow, params.signal);
    case 'kdj':  return calcKDJ(arr, params.k_period, params.k_smooth, params.d_smooth);
    case 'atr':  return { atr: calcATR(arr, params.period) };
    case 'obv':  return { obv: calcOBV(arr) };
    case 'wr':   return { wr: calcWR(arr, params.period) };
    default:     return {};
  }
}

// ── 指标管理 ──────────────────────────────────────────────────────────
function addIndicator(key, params) {
  const reg = INDICATOR_REGISTRY[key];
  if (!reg) return null;
  const id = window._indicatorsState.nextId++;
  // 合并默认参数
  const finalParams = {};
  for (const p of reg.params) {
    finalParams[p.key] = (params && params[p.key] != null) ? params[p.key] : p.default;
  }
  const inst = { id, key, params: finalParams, visible: true, _series: null, _subChart: null };
  window._indicatorsState.activeIndicators.push(inst);
  _createSeriesFor(inst);
  saveIndicatorSettings();
  return inst;
}

function removeIndicator(id) {
  const idx = window._indicatorsState.activeIndicators.findIndex(i => i.id === id);
  if (idx < 0) return;
  const inst = window._indicatorsState.activeIndicators[idx];
  _destroySeriesFor(inst);
  window._indicatorsState.activeIndicators.splice(idx, 1);
  saveIndicatorSettings();
}

function updateIndicatorParams(id, newParams) {
  const inst = window._indicatorsState.activeIndicators.find(i => i.id === id);
  if (!inst) return;
  Object.assign(inst.params, newParams);
  saveIndicatorSettings();
}

function toggleIndicatorVisible(id, visible) {
  const inst = window._indicatorsState.activeIndicators.find(i => i.id === id);
  if (!inst) return;
  inst.visible = visible;
  _applyVisible(inst);
  saveIndicatorSettings();
}

// ── 序列创建/销毁 ────────────────────────────────────────────────────
function _createSeriesFor(inst) {
  const reg = INDICATOR_REGISTRY[inst.key];
  const { chart, oscContainer } = _getCtx();
  if (!chart || !reg) return;

  if (reg.type === 'overlay') {
    // 主图叠加：用 addLineSeries
    inst._series = {};
    for (const out of reg.outputs) {
      if (out.isHistogram) {
        // 主图上一般不画 histogram，跳过（BOLL/MACD 的 hist 只在副图有意义）
        continue;
      }
      inst._series[out.key] = chart.addLineSeries({
        color: out.color,
        lineWidth: out.lineWidth,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
    }
  } else if (reg.type === 'oscillator' && oscContainer) {
    // 副图：创建独立 chart 实例
    const subChart = LightweightCharts.createChart(oscContainer, {
      layout: {
        background: { type: 'solid', color: '#131722' },
        textColor: '#d1d4dc',
        fontSize: 10,
      },
      grid: {
        vertLines: { color: '#1e222d' },
        horzLines: { color: '#1e222d' },
      },
      rightPriceScale: { borderColor: '#363c4e' },
      timeScale: { borderColor: '#363c4e', timeVisible: true, secondsVisible: false },
      width: oscContainer.clientWidth,
      height: reg.height || 120,
    });
    // 添加水平参考线
    if (reg.levels) {
      for (const lv of reg.levels) {
        subChart.addLineSeries({
          color: lv.color,
          lineWidth: 1,
          lineStyle: 1,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
        }).setData([]);
        // 用 priceLine 画水平线（更好的方式：直接对主 series 设 priceLines）
      }
    }
    inst._subChart = subChart;
    inst._series = {};
    for (const out of reg.outputs) {
      if (out.isHistogram) {
        inst._series[out.key] = subChart.addHistogramSeries({
          color: out.color,
          priceLineVisible: false,
          lastValueVisible: false,
        });
      } else {
        inst._series[out.key] = subChart.addLineSeries({
          color: out.color,
          lineWidth: out.lineWidth,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: true,
        });
      }
    }
    _syncSubChartTimeScale(subChart);
    _layoutOscillatorPanes();
  }
}

function _destroySeriesFor(inst) {
  // 副图：整个 subChart 销毁
  if (inst._subChart) {
    try { inst._subChart.remove(); } catch (_) {}
    inst._subChart = null;
  }
  // 主图叠加：用 chart.removeSeries(series) — 注意 series.remove() 在 lightweight-charts 不存在
  if (inst._series) {
    const { chart } = _getCtx();
    for (const k of Object.keys(inst._series)) {
      try {
        if (chart && inst._series[k]) {
          chart.removeSeries(inst._series[k]);
        }
      } catch (_) {}
    }
    inst._series = null;
  }
  _layoutOscillatorPanes();
}

// 强制清空所有指标数据（切换品种时调用，避免老数据残留）
function clearAllIndicatorData() {
  for (const inst of window._indicatorsState.activeIndicators) {
    if (!inst._series) continue;
    for (const k of Object.keys(inst._series)) {
      try { inst._series[k].setData([]); } catch (_) {}
    }
  }
}

function _applyVisible(inst) {
  if (inst._subChart) {
    inst._subChart.applyOptions({ visible: inst.visible });
    _layoutOscillatorPanes();
  }
  if (inst._series) {
    for (const k of Object.keys(inst._series)) {
      try { inst._series[k].applyOptions({ visible: inst.visible }); } catch (_) {}
    }
  }
}

// 重新布局副图容器（隐藏的副图不占高度）
function _layoutOscillatorPanes() {
  const { oscContainer } = _getCtx();
  if (!oscContainer) return;
  const visibleSubs = window._indicatorsState.activeIndicators.filter(
    i => i._subChart && i.visible
  );
  // 容器整体显隐
  oscContainer.style.display = visibleSubs.length > 0 ? 'flex' : 'none';
  // 调整每个 subChart 高度
  for (const inst of visibleSubs) {
    const reg = INDICATOR_REGISTRY[inst.key];
    const h = reg.height || 120;
    try {
      inst._subChart.applyOptions({ height: h });
    } catch (_) {}
  }
}

// 副图与主图时间轴联动
function _syncSubChartTimeScale(subChart) {
  const { chart } = _getCtx();
  if (!chart) return;
  // 主图 → 副图
  chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
    if (range) subChart.timeScale().setVisibleLogicalRange(range);
  });
  // 副图 → 主图（避免循环：先 unsubscribe，设完再 subscribe）
  let syncing = false;
  subChart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
    if (syncing || !range) return;
    syncing = true;
    chart.timeScale().setVisibleLogicalRange(range);
    syncing = false;
  });
}

// ── 渲染：根据 K 线数据计算并 setData ────────────────────────────────
function renderAllIndicators(bars) {
  for (const inst of window._indicatorsState.activeIndicators) {
    _renderOne(inst, bars);
  }
}

function _renderOne(inst, bars) {
  if (!inst._series) return;
  const reg = INDICATOR_REGISTRY[inst.key];
  const result = calculateIndicator(inst.key, bars, inst.params);
  for (const out of reg.outputs) {
    const data = result[out.key];
    const s = inst._series[out.key];
    if (!s) continue;
    if (out.isHistogram) {
      // histogram 需要颜色字段
      const histData = (data || []).map(d => ({
        time: d.time,
        value: d.value,
        color: d.value >= 0 ? '#26a69a' : '#ef5350',
      }));
      s.setData(histData);
    } else {
      s.setData(data || []);
    }
  }
  // 添加水平参考线（副图）
  if (inst._subChart && reg.levels) {
    // 取第一条 series 作为 priceLine 宿主
    const hostSeries = Object.values(inst._series)[0];
    if (hostSeries) {
      // 清掉旧的 priceLines
      if (!inst._levelLines) inst._levelLines = [];
      for (const pl of inst._levelLines) {
        try { hostSeries.removePriceLine(pl); } catch (_) {}
      }
      inst._levelLines = [];
      // 添加新的
      for (const lv of reg.levels) {
        const pl = hostSeries.createPriceLine({
          price: lv.value,
          color: lv.color,
          lineStyle: 1,
          lineWidth: 1,
          axisLabelVisible: true,
          title: '',
        });
        inst._levelLines.push(pl);
      }
    }
  }
}

// ── 持久化（localStorage） ───────────────────────────────────────────
function saveIndicatorSettings() {
  try {
    const data = window._indicatorsState.activeIndicators.map(i => ({
      key: i.key, params: i.params, visible: i.visible,
    }));
    localStorage.setItem('pa_active_indicators', JSON.stringify(data));
  } catch (_) {}
}

function loadIndicatorSettings() {
  try {
    const raw = localStorage.getItem('pa_active_indicators');
    if (!raw) return [];
    return JSON.parse(raw);
  } catch (_) { return []; }
}

// 初始化：恢复已保存的指标；首次访问（无保存）则种子默认指标
function initIndicators() {
  const saved = loadIndicatorSettings();
  if (saved.length === 0) {
    // 首次访问：默认启用 6 条 EMA（与原 chart.js 固定配置一致）+ MACD 副图
    const defaultEmas = [
      { period: 5,   color: '#b26cff', lineWidth: 1 },
      { period: 10,  color: '#ff9800', lineWidth: 1 },
      { period: 20,  color: '#ff5252', lineWidth: 2 },
      { period: 40,  color: '#ffeb3b', lineWidth: 2 },
      { period: 60,  color: '#26a69a', lineWidth: 2 },
      { period: 120, color: '#29b6f6', lineWidth: 3 },
    ];
    for (const cfg of defaultEmas) {
      addIndicator('ema', { period: cfg.period });
      // 覆盖默认颜色（用 EMA_CONFIG 原配色）
      const inst = window._indicatorsState.activeIndicators[window._indicatorsState.activeIndicators.length - 1];
      if (inst && inst._series && inst._series.ema) {
        try {
          inst._series.ema.applyOptions({ color: cfg.color, lineWidth: cfg.lineWidth });
        } catch (_) {}
      }
    }
    saveIndicatorSettings();
  } else {
    for (const s of saved) {
      if (INDICATOR_REGISTRY[s.key]) {
        addIndicator(s.key, s.params);
        if (s.visible === false) {
          const inst = window._indicatorsState.activeIndicators[window._indicatorsState.activeIndicators.length - 1];
          if (inst) toggleIndicatorVisible(inst.id, false);
        }
      }
    }
  }
}

// ── 模态 UI ───────────────────────────────────────────────────────────
function openIndicatorsModal() {
  const modal = document.getElementById('indicators-modal');
  if (!modal) return;
  _renderIndicatorsModal();
  modal.classList.remove('hidden');
}

function _renderIndicatorsModal() {
  const listAvail = document.getElementById('ind-available-list');
  const listActive = document.getElementById('ind-active-list');
  if (!listAvail || !listActive) return;

  // 分类渲染可用指标
  const categories = { trend: '趋势 Trend', momentum: '动量 Momentum', volatility: '波动 Volatility', volume: '量能 Volume' };
  let htmlAvail = '';
  for (const [catKey, catLabel] of Object.entries(categories)) {
    const items = Object.entries(INDICATOR_REGISTRY).filter(([k, v]) => v.category === catKey);
    if (!items.length) continue;
    htmlAvail += `<div class="ind-cat-title">${catLabel}</div>`;
    for (const [key, reg] of items) {
      htmlAvail += `<div class="ind-avail-item" data-key="${key}">
        <span class="ind-avail-name">${reg.name}</span>
        <span class="ind-type-badge ${reg.type}">${reg.type === 'overlay' ? '主图' : '副图'}</span>
        <button class="ind-add-btn" data-add-key="${key}" title="添加">＋</button>
      </div>`;
    }
  }
  listAvail.innerHTML = htmlAvail;

  // 渲染已启用指标
  const active = window._indicatorsState.activeIndicators;
  let htmlActive = '';
  if (!active.length) {
    htmlActive = '<div class="ind-empty">尚未启用任何指标。点击左侧 ＋ 添加。</div>';
  } else {
    for (const inst of active) {
      const reg = INDICATOR_REGISTRY[inst.key];
      const paramHtml = reg.params.map(p => {
        const v = inst.params[p.key];
        return `<label class="ind-param">
          <span>${p.label}</span>
          <input type="number" data-ind-id="${inst.id}" data-param="${p.key}"
                 value="${v}" min="${p.min}" max="${p.max}" step="${p.step || 1}">
        </label>`;
      }).join('');
      const outLegend = reg.outputs.map(o =>
        `<span class="ind-out-legend"><span class="ind-out-swatch" style="background:${o.color}"></span>${o.label}</span>`
      ).join('');
      htmlActive += `<div class="ind-active-item" data-id="${inst.id}">
        <div class="ind-active-head">
          <label class="ind-visible-toggle">
            <input type="checkbox" data-vis-id="${inst.id}" ${inst.visible ? 'checked' : ''}>
            <span class="ind-active-name">${reg.name}</span>
          </label>
          <button class="ind-remove-btn" data-rm-id="${inst.id}" title="移除">✕</button>
        </div>
        <div class="ind-params">${paramHtml}</div>
        <div class="ind-out-legend-row">${outLegend}</div>
      </div>`;
    }
  }
  listActive.innerHTML = htmlActive;

  _bindModalEvents();
}

function _bindModalEvents() {
  // 添加按钮
  document.querySelectorAll('.ind-add-btn').forEach(btn => {
    btn.onclick = () => {
      const key = btn.dataset.addKey;
      addIndicator(key);
      // 用最近一次 bars 重新渲染
      const bars = window._lastBars || [];
      renderAllIndicators(bars);
      _renderIndicatorsModal();
    };
  });
  // 移除按钮
  document.querySelectorAll('.ind-remove-btn').forEach(btn => {
    btn.onclick = () => {
      const id = parseInt(btn.dataset.rmId);
      removeIndicator(id);
      _renderIndicatorsModal();
    };
  });
  // 可见性切换
  document.querySelectorAll('input[data-vis-id]').forEach(cb => {
    cb.onchange = () => {
      const id = parseInt(cb.dataset.visId);
      toggleIndicatorVisible(id, cb.checked);
    };
  });
  // 参数编辑
  document.querySelectorAll('input[data-ind-id][data-param]').forEach(inp => {
    inp.onchange = () => {
      const id = parseInt(inp.dataset.indId);
      const param = inp.dataset.param;
      const v = parseFloat(inp.value);
      if (isNaN(v)) return;
      updateIndicatorParams(id, { [param]: v });
      const bars = window._lastBars || [];
      renderAllIndicators(bars);
    };
  });
}

// ── 对外 API（供 app.js 调用） ───────────────────────────────────────
window._indicatorsAPI = {
  // 注册主图与副图容器（由 app.js init 后调用）
  registerContext(mainChart, mainCandleSeries, oscContainer) {
    window._indicatorsState.mainChart = mainChart;
    window._indicatorsState.mainCandleSeries = mainCandleSeries;
    window._indicatorsState.oscContainer = oscContainer;
    // 恢复已保存的指标
    initIndicators();
  },
  // K 线更新时调用
  onBarsUpdated(bars) {
    window._lastBars = bars;
    renderAllIndicators(bars);
  },
  // 切换品种/交易所前调用，清空所有指标老数据
  clearAllData: clearAllIndicatorData,
  // 打开模态
  openModal: openIndicatorsModal,
  closeModal() {
    const m = document.getElementById('indicators-modal');
    if (m) m.classList.add('hidden');
  },
};
