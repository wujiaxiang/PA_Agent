// chart.js — K-line chart via Lightweight Charts
//
// 提供：
//   - createChart(container)            创建图表，返回 {chart, candleSeries, emaSeries}
//   - setBars(candleSeries, bars)       灌入 K 线（forming bar 半透明）
//   - setEma(emaSeries, bars, period=20) 计算 EMA 并叠加
//   - setDecisionOverlays(candleSeries, decision)  入场/止损/TP1/TP2 价格线
//   - setSupportResistance(candleSeries, levels)   支撑/阻力位价格线 + 区间
//   - setDirectionMarker(candleSeries, decision)   最新 bar 的 ▲/▼ 方向箭头
//   - setSeqMarkers(candleSeries, bars)            奇数 seq 序号标签
//   - fitView(chart, visibleBars=20)               显示最近 N 根 + Y 轴 padding
//   - clearOverlays(candleSeries)                  清空所有价格线与 markers

const EMA_PERIOD_DEFAULT = 20;
const FIT_VISIBLE_BARS = 20;

// ── 颜色常量 ─────────────────────────────────────────────────────────
const COLOR_UP = '#26a69a';
const COLOR_DOWN = '#ef5350';
const COLOR_EMA = '#ffc800';      // amber
const COLOR_SUPPORT = '#22c55e';  // green-500
const COLOR_RESISTANCE = '#f59e0b'; // amber-500
const COLOR_LONG = '#26a69a';
const COLOR_SHORT = '#ef5350';

// forming bar 半透明色（按收阴/收阳降饱和度）
const COLOR_UP_FORMING = 'rgba(38,166,154,0.45)';
const COLOR_DOWN_FORMING = 'rgba(239,83,80,0.45)';

// ── 全局状态：每个 candleSeries 关联的 overlays ─────────────────────
// 用 WeakMap 避免污染 series 对象
const _overlayState = new WeakMap();

function _getOverlayState(candleSeries) {
  let st = _overlayState.get(candleSeries);
  if (!st) {
    st = { priceLines: [], markers: [] };
    _overlayState.set(candleSeries, st);
  }
  return st;
}

// ── 创建图表 ─────────────────────────────────────────────────────────
function createChart(container) {
  const chart = LightweightCharts.createChart(container, {
    layout: {
      background: { type: 'solid', color: '#131722' },
      textColor: '#d1d4dc',
    },
    grid: {
      vertLines: { color: '#1e222d' },
      horzLines: { color: '#1e222d' },
    },
    crosshair: { mode: 0 },
    rightPriceScale: { borderColor: '#363c4e' },
    timeScale: { borderColor: '#363c4e', timeVisible: true },
  });

  const candleSeries = chart.addCandlestickSeries({
    upColor: COLOR_UP,
    downColor: COLOR_DOWN,
    borderUpColor: COLOR_UP,
    borderDownColor: COLOR_DOWN,
    wickUpColor: COLOR_UP,
    wickDownColor: COLOR_DOWN,
  });

  // EMA20 叠加线
  const emaSeries = chart.addLineSeries({
    color: COLOR_EMA,
    lineWidth: 2,
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false,
  });

  return { chart, candleSeries, emaSeries };
}

// ── K 线数据 ─────────────────────────────────────────────────────────
function setBars(candleSeries, bars) {
  if (!bars || !bars.length) {
    candleSeries.setData([]);
    return;
  }
  // 升序排列
  const sorted = [...bars].sort((a, b) => a.ts_open - b.ts_open);
  const data = sorted.map((b, i, arr) => {
    const isLast = i === arr.length - 1;
    // 最后一根且未收盘 → 半透明
    if (isLast && b.closed === false) {
      const color = b.close >= b.open ? COLOR_UP_FORMING : COLOR_DOWN_FORMING;
      return {
        time: b.ts_open / 1000,
        open: b.open, high: b.high, low: b.low, close: b.close,
        color,
        borderColor: color,
        wickColor: color,
      };
    }
    return {
      time: b.ts_open / 1000,
      open: b.open, high: b.high, low: b.low, close: b.close,
    };
  });
  candleSeries.setData(data);
}

// ── EMA 叠加 ────────────────────────────────────────────────────────
function setEma(emaSeries, bars, period = EMA_PERIOD_DEFAULT) {
  if (!bars || bars.length < period) {
    emaSeries.setData([]);
    return;
  }
  const sorted = [...bars].sort((a, b) => a.ts_open - b.ts_open);
  const k = 2 / (period + 1);
  // SMA 种子
  let emaPrev = 0;
  for (let i = 0; i < period; i++) emaPrev += sorted[i].close;
  emaPrev /= period;
  const out = [{ time: sorted[period - 1].ts_open / 1000, value: emaPrev }];
  for (let i = period; i < sorted.length; i++) {
    emaPrev = sorted[i].close * k + emaPrev * (1 - k);
    out.push({ time: sorted[i].ts_open / 1000, value: emaPrev });
  }
  emaSeries.setData(out);
}

// ── 决策价格线 ───────────────────────────────────────────────────────
function setDecisionOverlays(candleSeries, decision) {
  const st = _getOverlayState(candleSeries);
  // 清掉之前的决策价格线（保留 markers，由 setSeqMarkers/setDirectionMarker 管理）
  _clearPriceLines(candleSeries);

  if (!decision) return;
  // 不下单 或 显式关闭叠加层 → 不画
  if (decision.order_type === '不下单' || decision.order_type === 'no_order') return;
  if (decision.chart_overlay_active === false) return;

  const dir = String(decision.order_direction || '').toLowerCase();
  const isLong = dir === 'long' || dir === '做多' || dir === 'buy';
  const isShort = dir === 'short' || dir === '做空' || dir === 'sell';
  const dirColor = isShort ? COLOR_SHORT : COLOR_LONG;  // 默认按 long 色

  // lineStyle: 0=Solid, 1=Dotted, 2=Dashed, 3=LargeDashed, 4=SparseDotted
  const lines = [];
  if (_isNum(decision.entry_price)) {
    lines.push({ price: decision.entry_price, color: dirColor, title: 'Entry', lineStyle: 0, lineWidth: 2 });
  }
  if (_isNum(decision.stop_loss_price)) {
    lines.push({ price: decision.stop_loss_price, color: COLOR_DOWN, title: 'SL', lineStyle: 2, lineWidth: 1 });
  }
  if (_isNum(decision.take_profit_price)) {
    lines.push({ price: decision.take_profit_price, color: COLOR_UP, title: 'TP1', lineStyle: 2, lineWidth: 1 });
  }
  if (_isNum(decision.take_profit_price_2)) {
    lines.push({ price: decision.take_profit_price_2, color: COLOR_UP, title: 'TP2', lineStyle: 2, lineWidth: 1 });
  }
  for (const l of lines) {
    const pl = candleSeries.createPriceLine({
      price: l.price,
      color: l.color,
      lineStyle: l.lineStyle,
      lineWidth: l.lineWidth,
      axisLabelVisible: true,
      title: l.title,
    });
    st.priceLines.push(pl);
  }
}

// ── 支撑/阻力位 ──────────────────────────────────────────────────────
// levels: [{kind: 'support'|'resistance', low, high, label}, ...]
// 单点: low === high；区间: low < high
function setSupportResistance(candleSeries, levels) {
  const st = _getOverlayState(candleSeries);
  _clearPriceLines(candleSeries);

  if (!Array.isArray(levels) || !levels.length) return;

  for (const lv of levels) {
    if (!lv) continue;
    const low = _toNum(lv.low);
    const high = _toNum(lv.high);
    if (low == null || high == null) continue;
    const isSupport = String(lv.kind || '').toLowerCase().includes('support') ||
                      String(lv.label || '').includes('支撑');
    const color = isSupport ? COLOR_SUPPORT : COLOR_RESISTANCE;
    const title = lv.label || (isSupport ? '支撑' : '阻力');
    const isZone = Math.abs(high - low) > 1e-9;

    if (isZone) {
      // 区间：画上下两条线，下线虚线、上线实线
      const plLow = candleSeries.createPriceLine({
        price: low, color, lineStyle: 2, lineWidth: 1,
        axisLabelVisible: false, title: `${title} 低`,
      });
      const plHigh = candleSeries.createPriceLine({
        price: high, color, lineStyle: 0, lineWidth: 1,
        axisLabelVisible: true, title: `${title} 高`,
      });
      st.priceLines.push(plLow, plHigh);
    } else {
      const pl = candleSeries.createPriceLine({
        price: (low + high) / 2, color, lineStyle: 0, lineWidth: 1,
        axisLabelVisible: true, title,
      });
      st.priceLines.push(pl);
    }
  }
}

// ── 方向箭头 marker ──────────────────────────────────────────────────
function setDirectionMarker(candleSeries, decision) {
  const st = _getOverlayState(candleSeries);
  // 移除旧的方向 marker（保留 seq marker 由 setSeqMarkers 管理）
  st.markers = st.markers.filter(m => !m._isDirection);

  if (!decision) return;
  if (decision.order_type === '不下单' || decision.order_type === 'no_order') return;
  const dir = String(decision.order_direction || '').toLowerCase();
  const isLong = dir === 'long' || dir === '做多' || dir === 'buy';
  const isShort = dir === 'short' || dir === '做空' || dir === 'sell';
  if (!isLong && !isShort) return;

  // 拿最新一根 bar 的时间作为 marker 锚点
  const lastBar = candleSeries.dataByIndex
    ? null  // API 不直接暴露
    : null;
  // 通过 series 数据范围取最后一根
  const logicalRange = candleSeries?.options?._lastLogicalRange;  // 不一定可用
  // 退而求其次：从 chart.timeScale() 拿 visible range 的 to
  // 由于难以可靠拿到最后一根 bar 的 time，调用方需在 setBars 后通过 _lastBarTime 全局传递
  const t = (typeof window !== 'undefined' && window.__PA_LAST_BAR_TIME__) || null;
  if (t == null) return;

  const marker = {
    time: t,
    position: isLong ? 'belowBar' : 'aboveBar',
    color: isLong ? COLOR_LONG : COLOR_SHORT,
    shape: isLong ? 'arrowUp' : 'arrowDown',
    text: isLong ? '▲' : '▼',
    _isDirection: true,
  };
  st.markers.push(marker);
  _applyMarkers(candleSeries);
}

// ── 奇数 seq 序号标签 ────────────────────────────────────────────────
function setSeqMarkers(candleSeries, bars) {
  const st = _getOverlayState(candleSeries);
  st.markers = st.markers.filter(m => !m._isSeq);

  if (!bars || !bars.length) {
    _applyMarkers(candleSeries);
    return;
  }
  const sorted = [...bars].sort((a, b) => a.ts_open - b.ts_open);
  // 仅在最近 FIT_VISIBLE_BARS*2 范围内画奇数 seq，避免太密
  const start = Math.max(0, sorted.length - FIT_VISIBLE_BARS * 2);
  for (let i = start; i < sorted.length; i++) {
    const b = sorted[i];
    if (typeof b.seq !== 'number') continue;
    if (b.seq <= 0) continue;          // 0 = forming bar
    if (b.seq % 2 === 0) continue;     // 仅奇数
    st.markers.push({
      time: b.ts_open / 1000,
      position: 'aboveBar',
      color: '#9ca3af',
      shape: 'circle',
      text: `#${b.seq}`,
      _isSeq: true,
    });
  }
  _applyMarkers(candleSeries);
}

// ── fit_view ─────────────────────────────────────────────────────────
function fitView(chart, visibleBars = FIT_VISIBLE_BARS) {
  // 显示最近 visibleBars 根 + 右侧留 0.5 根 padding
  // 由于 lightweight-charts 的 logical range 以数据点 0 开始，
  // 我们通过 setData 后调用 scrollToRealTime + setVisibleLogicalRange
  try {
    chart.timeScale().scrollToRealTime();
    // 设置可见逻辑范围：从 -visibleBars 到 0.5（相对当前最右）
    chart.timeScale().setVisibleLogicalRange({ from: -visibleBars, to: 0.5 });
  } catch (e) {
    // 退而求其次
    try { chart.timeScale().fitContent(); } catch (_) {}
  }
  // Y 轴 padding 通过 price scale 的 scaleMargins 实现
  try {
    chart.priceScale('right').applyOptions({
      scaleMargins: { top: 0.07, bottom: 0.07 },
    });
  } catch (_) {}
}

// ── 清空 overlays ───────────────────────────────────────────────────
function clearOverlays(candleSeries) {
  _clearPriceLines(candleSeries);
  const st = _getOverlayState(candleSeries);
  st.markers = [];
  _applyMarkers(candleSeries);
}

function _clearPriceLines(candleSeries) {
  const st = _getOverlayState(candleSeries);
  for (const pl of st.priceLines) {
    try { candleSeries.removePriceLine(pl); } catch (_) {}
  }
  st.priceLines = [];
}

function _applyMarkers(candleSeries) {
  const st = _getOverlayState(candleSeries);
  // lightweight-charts 要求 markers 按 time 升序
  const sorted = [...st.markers].sort((a, b) => {
    const ta = typeof a.time === 'number' ? a.time : 0;
    const tb = typeof b.time === 'number' ? b.time : 0;
    return ta - tb;
  });
  // 去掉内部字段 _isSeq/_isDirection 再交给 SDK
  const clean = sorted.map(m => ({
    time: m.time, position: m.position, color: m.color,
    shape: m.shape, text: m.text,
  }));
  try {
    candleSeries.setMarkers(clean);
  } catch (_) {}
}

function _isNum(v) {
  return typeof v === 'number' && !isNaN(v) && isFinite(v);
}
function _toNum(v) {
  if (typeof v === 'number' && !isNaN(v)) return v;
  if (typeof v === 'string') {
    const n = parseFloat(v);
    return isNaN(n) ? null : n;
  }
  return null;
}
