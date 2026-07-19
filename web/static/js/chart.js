// chart.js — K-line chart via Lightweight Charts
//
// 提供：
//   - createChart(container)            创建图表，返回 {chart, candleSeries}
//   - setBars(candleSeries, bars)       灌入 K 线（forming bar 半透明）
//   - setDecisionOverlays(candleSeries, decision)  入场/止损/TP1/TP2 价格线
//   - setSupportResistance(candleSeries, levels)   支撑/阻力位价格线 + 区间
//   - setDirectionMarker(candleSeries, decision)   最新 bar 的 ▲/▼ 方向箭头
//   - setSeqMarkers(candleSeries, bars)            奇数 seq 序号标签
//   - fitView(chart, visibleBars=20)               显示最近 N 根 + Y 轴 padding
//   - clearOverlays(candleSeries)                  清空所有价格线与 markers
//
// 注：EMA 均线已迁移到 indicators.js（统一指标管理器），不再在此固定渲染。

const FIT_VISIBLE_BARS = 20;

// ── 颜色常量 ─────────────────────────────────────────────────────────
const COLOR_UP = '#26a69a';
const COLOR_DOWN = '#ef5350';
const COLOR_EMA = '#ffc800';      // amber（保留供旧代码引用）
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

// ── 时间格式化（中文） ───────────────────────────────────────────────
// Lightweight Charts 内部时间戳是秒（UNIX epoch UTC）。
// 默认显示 UTC，这里统一转成显式时区（_displayTimezone）并用中文习惯格式化。
// 当 _displayTimezone 为空时回退到浏览器本地时区（旧行为）。

// 模块级显式显示时区（IANA 名称，如 "Asia/Shanghai"）。空串 = 浏览器本地。
let _displayTimezone = "Asia/Shanghai";

/**
 * 设置显示时区。可任意时刻调用，idempotent。
 * 无效时区会被捕获并回退为空串（浏览器本地）。
 */
function setDisplayTimezone(tz) {
  _displayTimezone = tz || "";
  // 通过尝试构造 Intl.DateTimeFormat 验证时区字符串
  try {
    if (_displayTimezone) {
      new Intl.DateTimeFormat("zh-CN", { timeZone: _displayTimezone });
    }
  } catch (e) {
    _displayTimezone = "";  // 无效，回退到浏览器本地
  }
}

/**
 * 把秒级 UNIX 时间戳格式化为 "YYYY/MM/DD HH:mm:ss"（显式时区）。
 * 用于十字光标悬浮提示。当 _displayTimezone 为空时回退到浏览器本地。
 */
function _fmtLocalTime(sec) {
  const d = new Date(sec * 1000);
  if (_displayTimezone) {
    try {
      return new Intl.DateTimeFormat("zh-CN", {
        timeZone: _displayTimezone,
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit",
        hour12: false,
      }).format(d);
    } catch (e) { /* 落到浏览器本地分支 */ }
  }
  return d.toLocaleString("zh-CN", { hour12: false });
}

/**
 * 时间轴刻度格式化器：根据 tickMarkType 决定显示粒度。
 * tickMarkType: 0=Year, 1=Month, 2=DayOfMonth, 3=Time, 4=TimeWithSeconds
 * 策略：
 *   - 年/月：显示 "YYYY年" / "YYYY年MM月"（仅在跨度大的图表用）
 *   - 日：显示 "MM-DD"
 *   - 时分：显示 "HH:mm"
 * 当 _displayTimezone 非空时，通过 Intl.DateTimeFormat 路由到显式时区；
 * 否则回退到 new Date() 的浏览器本地行为。
 */
function _tickMarkFormatter(time, tickMarkType, locale) {
  const d = new Date(time * 1000);

  // 通过 Intl.formatToParts 取出指定时区下的字段
  const partsMap = (() => {
    if (!_displayTimezone) return null;
    try {
      const parts = new Intl.DateTimeFormat("zh-CN", {
        timeZone: _displayTimezone,
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit",
        hour12: false,
      }).formatToParts(d);
      const m = {};
      for (const p of parts) m[p.type] = p.value;
      // hour12:false 下部分浏览器返回 "24"，规整为 "00"
      if (m.hour === "24") m.hour = "00";
      return m;
    } catch (e) {
      return null;
    }
  })();

  // 无显式时区 → 走原浏览器本地逻辑
  if (!partsMap) {
    switch (tickMarkType) {
      case 0: // Year
        return `${d.getFullYear()}年`;
      case 1: // Month
        return `${d.getFullYear()}年${String(d.getMonth() + 1).padStart(2, '0')}月`;
      case 2: // DayOfMonth
        return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
      case 3: // Time (HH:mm)
      case 4: // TimeWithSeconds
        return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
      default:
        return _fmtLocalTime(time);
    }
  }

  // 显式时区分支：保持与原格式一致，仅替换字段来源
  switch (tickMarkType) {
    case 0: // Year
      return `${partsMap.year}年`;
    case 1: // Month
      return `${partsMap.year}年${partsMap.month}月`;
    case 2: // DayOfMonth
      return `${partsMap.month}-${partsMap.day}`;
    case 3: // Time (HH:mm)
    case 4: // TimeWithSeconds
      return `${partsMap.hour}:${partsMap.minute}`;
    default:
      return _fmtLocalTime(time);
  }
}

// 暴露 setDisplayTimezone 供 app.js 调用
window._chartAPI = window._chartAPI || {};
window._chartAPI.setDisplayTimezone = setDisplayTimezone;

// ── 创建图表 ─────────────────────────────────────────────────────────
function createChart(container) {
  const chart = LightweightCharts.createChart(container, {
    layout: {
      background: { type: 'solid', color: '#131722' },
      textColor: '#d1d4dc',
      // 中文友好字体栈
      fontFamily: '"PingFang SC", "Microsoft YaHei", "Hiragino Sans GB", "Noto Sans CJK SC", sans-serif',
      locale: 'zh-CN',
    },
    grid: {
      vertLines: { color: '#1e222d' },
      horzLines: { color: '#1e222d' },
    },
    crosshair: { mode: 0 },
    rightPriceScale: { borderColor: '#363c4e' },
    timeScale: {
      borderColor: '#363c4e',
      timeVisible: true,
      secondsVisible: false,
      // 时间轴刻度格式化（中文）
      tickMarkFormatter: _tickMarkFormatter,
      // 右侧留出最后一根 bar 的呼吸空间
      rightOffset: 4,
    },
    localization: {
      // 十字光标悬浮时顶部的时间显示
      timeFormatter: _fmtLocalTime,
      // 价格格式：默认 2 位小数，加密货币大价格自动适配
      priceFormatter: _priceFormatter,
    },
  });

  const candleSeries = chart.addCandlestickSeries({
    upColor: COLOR_UP,
    downColor: COLOR_DOWN,
    borderUpColor: COLOR_UP,
    borderDownColor: COLOR_DOWN,
    wickUpColor: COLOR_UP,
    wickDownColor: COLOR_DOWN,
  });

  // EMA 均线由 indicators.js 统一管理（可由用户在「📐 指标」面板自由添加/配置）
  // 保留 emaSeriesMap/emaSeries 占位以兼容旧引用（值为 null/{}）
  const emaSeriesMap = {};
  const emaSeries = null;

  return { chart, candleSeries, emaSeries, emaSeriesMap };
}

/**
 * 价格格式化器：根据价格量级自动决定小数位数。
 * < 1: 4 位小数（外汇/交叉盘）
 * < 100: 2 位小数（多数股票/加密货币）
 * >= 100: 2 位小数（加密货币大价格）
 */
function _priceFormatter(price) {
  if (typeof price !== 'number' || !isFinite(price)) return '—';
  if (price < 1) return price.toFixed(4);
  return price.toFixed(2);
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

// ── EMA 渲染已迁移到 indicators.js（统一指标管理器） ─────────────────

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

// ── seq 序号标签 ─────────────────────────────────────────────────────
// 根据可见 bar 总数自适应步长，保证标签不重叠又尽量覆盖所有 bar。
//   总数 ≤ 30：每根都画（1,2,3,...）
//   总数 ≤ 60：每 2 根（1,3,5,...）
//   总数 ≤ 120：每 3 根（1,4,7,...）
//   总数 > 120：每 5 根（1,6,11,...）
function _seqStep(total) {
  if (total <= 30) return 1;
  if (total <= 60) return 2;
  if (total <= 120) return 3;
  return 5;
}

function setSeqMarkers(candleSeries, bars) {
  const st = _getOverlayState(candleSeries);
  st.markers = st.markers.filter(m => !m._isSeq);

  if (!bars || !bars.length) {
    _applyMarkers(candleSeries);
    return;
  }
  const sorted = [...bars].sort((a, b) => a.ts_open - b.ts_open);
  const step = _seqStep(sorted.length);
  for (let i = 0; i < sorted.length; i++) {
    const b = sorted[i];
    if (typeof b.seq !== 'number') continue;
    if (b.seq <= 0) continue;          // 0 = forming bar，不画序号
    // 步长过滤：仅画 1, 1+step, 1+2*step, ...
    if ((b.seq - 1) % step !== 0) continue;
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
// 显示最近 visibleBars 根 K 线，自动滚动到最新一根。
// totalBars: 数据总根数（可选，传入后可精确控制可见范围；不传则仅 scrollToRealTime）
function fitView(chart, visibleBars = FIT_VISIBLE_BARS, totalBars = 0) {
  try {
    if (totalBars > 0 && totalBars > visibleBars) {
      // setVisibleLogicalRange 的 from/to 是绝对逻辑索引：
      //   0 = 第一根 bar, totalBars-1 = 最后一根 bar
      // 要显示最后 visibleBars 根 + 右侧留 1.5 根 padding：
      //   from = totalBars - visibleBars - 1
      //   to   = totalBars - 1 + 1.5  （+1.5 = rightOffset 补偿）
      chart.timeScale().setVisibleLogicalRange({
        from: totalBars - visibleBars - 1,
        to: totalBars - 1 + 1.5,
      });
    } else {
      // 数据不足或未传入总数 → 直接滚动到最新
      chart.timeScale().scrollToRealTime();
    }
  } catch (e) {
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
