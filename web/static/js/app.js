// app.js — PA Agent Web main UI

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

// ── Mermaid.js 初始化（决策树流程图，Phase K Task 21） ────────────────
if (typeof mermaid !== 'undefined') {
  mermaid.initialize({ startOnLoad: false, theme: 'dark', securityLevel: 'loose' });
}

// ── Cycle 8 keys 中文标签（与 pa_agent/ai/cycle_enums.py:CYCLE_POSITION_ZH 对齐，单一来源） ─────
const CYCLE_LABELS = {
  spike: '尖峰 (Spike)',
  micro_channel: '微型通道',
  tight_channel: '窄通道',
  normal_channel: '正常通道',
  broad_channel: '宽通道',
  trending_tr: '趋势型交易区间',
  trading_range: '交易区间',
  extreme_tr: '极端交易区间',
  unknown: '未知',
};

// Range-style cycles: structure is sideways; direction refines the bias.
// 与 pa_agent/ai/cycle_enums.py:RANGE_DISPLAY_CYCLES 对齐（单一来源）
const RANGE_DISPLAY_CYCLES = new Set(['trading_range', 'extreme_tr', 'trending_tr']);

// ── 中英对照表（决策卡片字段值翻译，显示为"中文 (English)"） ────────
const DIRECTION_ZH = {
  bullish: '看涨', bearish: '看跌', neutral: '中性',
  long: '做多', short: '做空', buy: '做多', sell: '做空',
  多头: '多头', 空头: '空头', 做多: '做多', 做空: '做空', 看涨: '看涨', 看跌: '看跌', 中性: '中性',
};
const GATE_RESULT_ZH = {
  proceed: '继续', wait: '等待', reject: '放弃', unknown: '未知',
  pass: '通过', fail: '未通过',
};
const RISK_LEVEL_ZH = {
  high: '高', medium: '中', low: '低', 高: '高', 中: '中', 低: '低',
};
const VOLATILITY_ZH = {
  high: '高', low: '低', medium: '中', extreme: '极端',
  expanding: '扩张', contracting: '收缩', stable: '稳定',
  elevated: '偏高', normal: '正常',
};
const MARKET_PHASE_ZH = {
  // GUI 来源（pa_agent/gui/decision_panel.py:_MARKET_PHASE_ZH）
  stable: '稳定',
  transitioning: '过渡',
  // web 原有（保留，向后兼容）
  trending: '趋势',
  ranging: '震荡',
  transition: '过渡',
  accumulation: '积累',
  distribution: '派发',
  markup: '拉升',
  markdown: '下跌',
  breakout: '突破',
  reversal: '反转',
  pullback: '回调',
};
const ORDER_TYPE_ZH = {
  no_order: '不下单', market: '市价单', limit: '限价单', stop: '停损单',
  buy: '买入', sell: '卖出', long: '做多', short: '做空',
  breakout: '突破单',
};

// 通用中英对照：value 是英文 key 时返回 "中文 (English)"；中文或未知值原样返回
function bilingual(value, map) {
  if (value == null) return '';
  const s = String(value).trim();
  if (!s) return '';
  const lower = s.toLowerCase();
  if (map[lower] != null) {
    const zh = map[lower];
    // 若原值就是中文且与翻译相同，直接返回（避免"看涨 (看涨)"）
    if (s === zh) return s;
    return `${zh} (${s})`;
  }
  return s;
}

// 周期位置专用翻译（用 CYCLE_LABELS）
// 与 GUI pa_agent/ai/cycle_enums.py:format_cycle_position 对齐：返回纯中文 label（CYCLE_LABELS 已包含所需双语形式，如 '尖峰 (Spike)'）
function bilingualCycle(value) {
  if (value == null) return '';
  const s = String(value).trim();
  if (!s) return '';
  const lower = s.toLowerCase();
  const zh = CYCLE_LABELS[lower];
  return zh || s;
}

// ── 派生 helper（复刻 GUI pa_agent/ai/cycle_enums.py 的派生逻辑） ───────
// 复刻 format_trend_label(direction, cycle_position)：返回 "上涨/下跌/震荡/震荡偏多/震荡偏空/趋势运行中/—"
function formatTrendLabel(direction, cyclePosition) {
  const cp = (cyclePosition || '').trim().toLowerCase();
  const d = (direction || '').trim().toLowerCase();
  if (RANGE_DISPLAY_CYCLES.has(cp)) {
    if (d === 'bullish') return '震荡偏多';
    if (d === 'bearish') return '震荡偏空';
    return '震荡';
  }
  if (d === 'bullish') return '上涨';
  if (d === 'bearish') return '下跌';
  if (d === 'neutral') return '震荡';
  if (cp === 'spike' || cp === 'micro_channel' || cp === 'tight_channel') return '趋势运行中';
  return '—';
}

// 复刻 format_cycle_with_direction(cycle_position, direction)：返回 "上涨宽通道" 等
function formatCycleWithDirection(cyclePosition, direction) {
  const base = bilingualCycle(cyclePosition) || '—';
  const cp = (cyclePosition || '').trim().toLowerCase();
  if (!cp || cp === 'unknown') return base;
  const d = (direction || '').trim().toLowerCase();
  const prefix = { bullish: '上涨', bearish: '下跌', neutral: '震荡' }[d] || '';
  return prefix ? `${prefix}${base}` : base;
}

// 派生 trend label 的颜色（接收中文 label "上涨/下跌/震荡偏多/震荡偏空/震荡/趋势运行中"）
// 复刻 GUI pa_agent/gui/decision_panel.py:_trend_color
function trendLabelColor(label) {
  if (!label) return '';
  if (label === '上涨' || label === '震荡偏多') return '#26a69a';
  if (label === '下跌' || label === '震荡偏空') return '#ef5350';
  if (label === '震荡' || label === '趋势运行中') return '#ffc800';
  return '';
}

// ── Globals ────────────────────────────────────────────────────────────
let chart, candleSeries, emaSeries, emaSeriesMap;
let lastRecord = null;
let isReplaying = false;
let currentSettings = null;
let lastBars = null;           // 最近一次 /api/bars 返回的 K 线（供实时刷新复用）
let liveRefreshTimer = null;   // 实时刷新 setInterval 句柄（fallback 轮询使用）
let liveRefreshLastTs = 0;     // 上次刷新时间戳（ms）
let sseBarsStream = null;      // SSE EventSource 实例 (/api/bars/stream)
let sseReconnectTimer = null;  // SSE 重连定时器（10s 退避）
let sseFallbackPolling = false;// SSE 失败后是否降级为轮询模式
let sseLastBarUpdateTs = 0;    // 最近一次 SSE bar_update/bar_close 事件的时间戳（ms）
let sseNextCloseTs = 0;        // 当前 forming bar 的下一收盘时间戳（ms，来自后端 next_close_ts）
let sseStatusExpiryTimer = null;  // updateSSEStatusWithExpiry 定时器句柄
let displayTimezone = "Asia/Shanghai";  // 显示时区（IANA 名称），与 chart.js _displayTimezone 同步
let isAnalyzing = false;                // 是否有分析进行中（供「持续跟踪」开关判断）
let waitCloseCountdownTimer = null;     // 等待收盘 setInterval 句柄

// ── 追问嵌入实时 tab（Phase C Task 3） ──────────────────────────────────
// chatAbortController: 追问 SSE 的 AbortController，非 null 表示发送中
// chatReasoningText / chatContentText: 当前追问 AI 消息的累计文本
// lastUserMessage: 上一条用户追问文本（供 resendLastChat 重发）
// stageCharCounts: 各阶段 reasoning/content 字数统计（供 #stream-stats 显示）
let chatAbortController = null;
let chatReasoningText = '';
let chatContentText = '';
let lastUserMessage = '';
let stageCharCounts = { stage1: { reasoning: 0, content: 0 }, stage2: { reasoning: 0, content: 0 }, chat: { reasoning: 0, content: 0 } };

// ── 侧边栏可调宽度（Phase A Task 1） ──────────────────────────────────
// 拖拽 .sidebar-resizer 调整 #sidebar 宽度（360-900px），持久化到 localStorage
function initSidebarResizer() {
  const resizer = $('#sidebar-resizer');
  const sidebar = $('#sidebar');
  if (!resizer || !sidebar) return;

  const MIN_WIDTH = 360;
  const MAX_WIDTH = 900;
  const STORAGE_KEY = 'pa_sidebar_width';

  // 恢复上次宽度
  const savedWidth = localStorage.getItem(STORAGE_KEY);
  if (savedWidth) {
    const w = parseInt(savedWidth);
    if (w >= MIN_WIDTH && w <= MAX_WIDTH) {
      sidebar.style.width = w + 'px';
    }
  }

  let isDragging = false;
  let startX = 0;
  let startWidth = 0;

  resizer.addEventListener('mousedown', (e) => {
    isDragging = true;
    startX = e.clientX;
    startWidth = sidebar.offsetWidth;
    resizer.classList.add('dragging');
    document.body.style.userSelect = 'none';
    document.body.style.cursor = 'col-resize';
    e.preventDefault();
  });

  document.addEventListener('mousemove', (e) => {
    if (!isDragging) return;
    // sidebar 在右侧，鼠标向左拖动 = 宽度增加
    const delta = startX - e.clientX;
    let newWidth = startWidth + delta;
    if (newWidth < MIN_WIDTH) newWidth = MIN_WIDTH;
    if (newWidth > MAX_WIDTH) newWidth = MAX_WIDTH;
    sidebar.style.width = newWidth + 'px';
    // 同步 K 线画布尺寸（lightweight-charts 不会自动响应容器尺寸变化）
    if (typeof resizeChart === 'function') resizeChart();
  });

  document.addEventListener('mouseup', () => {
    if (!isDragging) return;
    isDragging = false;
    resizer.classList.remove('dragging');
    document.body.style.userSelect = '';
    document.body.style.cursor = '';
    const finalWidth = sidebar.offsetWidth;
    if (finalWidth >= MIN_WIDTH && finalWidth <= MAX_WIDTH) {
      localStorage.setItem(STORAGE_KEY, String(finalWidth));
    }
    // 释放后再同步一次，确保最终尺寸对齐
    if (typeof resizeChart === 'function') resizeChart();
  });

  // 兜底：用 ResizeObserver 监听 chart-container 尺寸变化（处理折叠/展开、窗口分屏等场景）
  const chartContainer = $('#chart-container');
  if (chartContainer && typeof ResizeObserver !== 'undefined') {
    const ro = new ResizeObserver(() => {
      if (typeof resizeChart === 'function') resizeChart();
    });
    ro.observe(chartContainer);
  }
}

// ── Init ───────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  initSidebarResizer();  // 恢复侧边栏宽度（Phase A Task 1），需在 createChart 前完成以避免布局抖动
  const { chart: c, candleSeries: cs, emaSeries: es, emaSeriesMap: em } = createChart($('#chart-container'));
  chart = c; candleSeries = cs; emaSeries = es; emaSeriesMap = em;

  await loadSettings();      // 先加载 settings，回填交易所/品种/周期
  await loadExchanges();     // 再加载交易所下拉（并选中当前值）
  await loadSymbols();       // 根据当前交易所加载品种列表
  await loadTimeframes();    // 加载周期下拉（并选中当前值）
  await loadBars();          // 最后拉 K 线
  loadHistoryList();         // 拉当前 (exchange, symbol, timeframe) 的历史分析记录
  refreshIncrementalButtonState();  // 初始化增量分析按钮可用性

  bindEvents();
  // 注册指标库上下文（主图 + 副图容器），恢复已保存的指标
  if (window._indicatorsAPI) {
    const oscWrap = document.getElementById('chart-osc-wrap');
    window._indicatorsAPI.registerContext(chart, candleSeries, oscWrap);
    // 首次数据已加载，触发指标渲染
    window._indicatorsAPI.onBarsUpdated(lastBars || []);
  }
  resizeChart();
});

window.addEventListener('resize', resizeChart);

function resizeChart() {
  const el = $('#chart-container');
  if (chart) chart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
  // 副图容器宽度跟随主图（高度由各 subChart 自己 applyOptions）
  const oscWrap = document.getElementById('chart-osc-wrap');
  if (oscWrap) {
    const parentWidth = (el.parentElement || el).clientWidth;
    oscWrap.style.width = parentWidth + 'px';
    // 通知每个副图调整宽度
    if (window._indicatorsState) {
      for (const inst of window._indicatorsState.activeIndicators) {
        if (inst._subChart) {
          try { inst._subChart.applyOptions({ width: parentWidth }); } catch (_) {}
        }
      }
    }
  }
}

// ── Data loading ───────────────────────────────────────────────────────
async function loadBars() {
  try {
    const data = await API.get('/api/bars?count=100');
    lastBars = data.bars || [];
    setBars(candleSeries, lastBars);
    setSeqMarkers(candleSeries, lastBars);
    // 记录最新一根 bar 的时间，供 setDirectionMarker 锚定
    if (lastBars.length) {
      const sorted = [...lastBars].sort((a, b) => a.ts_open - b.ts_open);
      window.__PA_LAST_BAR_TIME__ = sorted[sorted.length - 1].ts_open / 1000;
    }
    // 通知指标库重新计算并渲染（含 EMA/SMA/BOLL/RSI/MACD/KDJ 等）
    if (window._indicatorsAPI) window._indicatorsAPI.onBarsUpdated(lastBars);
    fitView(chart, 20, lastBars.length);
    liveRefreshLastTs = Date.now();
    updateLiveRefreshStatus();
  } catch (e) {
    console.error('loadBars:', e);
  }
}

// 仅刷新 K 线数据（用于实时刷新，不重置 overlay）
async function refreshBarsOnly() {
  try {
    const data = await API.get('/api/bars?count=100');
    lastBars = data.bars || [];
    setBars(candleSeries, lastBars);
    setSeqMarkers(candleSeries, lastBars);
    if (lastBars.length) {
      const sorted = [...lastBars].sort((a, b) => a.ts_open - b.ts_open);
      window.__PA_LAST_BAR_TIME__ = sorted[sorted.length - 1].ts_open / 1000;
    }
    // 通知指标库重新计算并渲染
    if (window._indicatorsAPI) window._indicatorsAPI.onBarsUpdated(lastBars);
    liveRefreshLastTs = Date.now();
    updateLiveRefreshStatus();
  } catch (e) {
    console.error('refreshBarsOnly:', e);
  }
}

async function loadSettings() {
  try {
    const s = await API.get('/api/settings');
    currentSettings = s;
    $('#s-base-url').value = s.provider?.base_url || '';
    $('#s-model').value = s.provider?.model || '';
    $('#s-api-key').value = s.provider?.api_key || '';
    $('#s-reasoning-effort').value = s.provider?.reasoning_effort || 'high';
    $('#s-thinking').checked = s.provider?.thinking !== false;
    $('#s-refresh-ms').value = s.general?.refresh_interval_ms || 1000;
    $('#s-decision-stance').value = s.general?.decision_stance || 'balanced';
    $('#s-ctx-warn').value = s.general?.context_warning_threshold_pct || 80;
    $('#ds-bar-count').value = s.general?.analysis_bar_count || 100;
    // 显示时区：读取并应用到 chart.js + 时区标签
    displayTimezone = s.general?.display_timezone || "Asia/Shanghai";
    const tzInput = $('#s-display-timezone');
    if (tzInput) tzInput.value = displayTimezone;
    if (window._chartAPI?.setDisplayTimezone) {
      window._chartAPI.setDisplayTimezone(displayTimezone);
    }
    updateTimezoneLabel(displayTimezone);
    // 回填工具栏的交易所/品种/周期（loadExchanges/loadTimeframes 会读取这些值选中）
    $('#ds-symbol').value = s.general?.last_symbol || 'BTCUSDT';
    if (s.general?.last_tradingview_exchange) {
      $('#ds-exchange').dataset.current = s.general.last_tradingview_exchange;
    }
    if (s.general?.last_timeframe) {
      $('#ds-timeframe').dataset.current = s.general.last_timeframe;
    }
    // 飞书设置
    const fs = s.feishu || {};
    $('#s-feishu-enabled').checked = fs.enabled !== false;
    $('#s-feishu-webhook').value = fs.webhook_url || '';
    $('#s-feishu-secret').value = fs.secret || '';
    $('#s-feishu-app-id').value = fs.app_id || '';
    $('#s-feishu-app-secret').value = fs.app_secret || '';
    $('#s-feishu-order-only').checked = fs.notify_on_order_only !== false;
    // Phase I Task 19: 回填通用 tab 新增字段
    const g = s.general || {};
    const setNum = (sel, val, def) => { const el = $(sel); if (el) el.value = (val == null || val === '') ? def : val; };
    setChecked('#s-auto-resume-chart', g.auto_resume_chart_after_analysis);
    setNum('#s-incremental-max-new-bars', g.incremental_max_new_bars, 10);
    setChecked('#s-keep-analysis', g.keep_analysis);
    setChecked('#s-cancel-keep-on-retry', g.cancel_keep_analysis_on_retry);
    setChecked('#s-predict-next-bar', g.enable_next_bar_prediction);
    setNum('#s-decision-confidence-threshold', g.decision_confidence_threshold, 40);
    setChecked('#s-alert-on-order-opportunity', g.alert_on_order_opportunity);
    setNum('#s-stream-font-size', g.stream_pane_font_pt, 11);
    setNum('#s-chart-seq-font-size', g.chart_seq_label_font_pt, 11);
    setNum('#s-decision-tree-play-duration', g.decision_flow_play_seconds, 50);
    setNum('#s-decision-tree-default-zoom', g.decision_flow_default_zoom_pct, 600);
    setChecked('#s-decision-tree-autoplay', g.decision_flow_auto_play);
    // context_window 在 provider 段
    setNum('#s-context-window', s.provider?.context_window, 2000000);
    // 第三方：pushplus / tushare
    const pp = s.pushplus || {};
    $('#s-pushplus-token').value = pp.token || '';
    setChecked('#s-pushplus-enabled', pp.enabled);
    const ts = s.tushare || {};
    $('#s-tushare-token').value = ts.token || '';
    // API Key 未配置警告：检查 provider.api_key_encrypted 是否为空字符串
    updateApiKeyAlert(s);
    return s;
  } catch (e) {
    console.error('loadSettings:', e);
  }
}

// 设置弹窗 checkbox 回填 helper（容错：元素不存在时跳过）
function setChecked(sel, val) {
  const el = $(sel);
  if (el) el.checked = !!val;
}

// 检查 API Key 是否已配置；未配置则显示顶部红色横幅
function updateApiKeyAlert(settings) {
  const alertEl = $('#api-key-alert');
  if (!alertEl) return;
  // 兼容 api_key_encrypted（持久化字段）与 api_key（运行时字段）
  const apiKeyEnc = settings?.provider?.api_key_encrypted;
  const apiKey = settings?.provider?.api_key;
  const configured = (typeof apiKeyEnc === 'string' && apiKeyEnc.length > 0)
                  || (typeof apiKey === 'string' && apiKey.length > 0);
  if (!configured) {
    alertEl.removeAttribute('hidden');
  } else {
    alertEl.setAttribute('hidden', '');
  }
}

async function loadExchanges() {
  try {
    const list = await API.get('/api/tv/exchanges');
    const sel = $('#ds-exchange');
    const cur = sel.dataset.current || '';
    sel.innerHTML = list.map(d => `<option value="${d.id}"${d.id === cur ? ' selected' : ''}>${d.label}</option>`).join('');
  } catch (e) {
    console.error('loadExchanges:', e);
  }
}

async function loadSymbols() {
  try {
    const exchange = $('#ds-exchange').value || '';
    const data = await API.get(`/api/tv/symbols?exchange=${encodeURIComponent(exchange)}`);
    const syms = data.symbols || [];
    // 同时填充 select（默认下拉）和 datalist（自定义输入提示）
    const sel = $('#ds-symbol-select');
    const dl = $('#ds-symbol-options');
    const curSymbol = $('#ds-symbol').value || (currentSettings?.general?.last_symbol || 'BTCUSDT');
    if (sel) {
      sel.innerHTML = syms.map(s => `<option value="${s}"${s === curSymbol ? ' selected' : ''}>${s}</option>`).join('');
    }
    if (dl) {
      dl.innerHTML = syms.map(s => `<option value="${s}">`).join('');
    }
  } catch (e) {
    console.error('loadSymbols:', e);
  }
}

async function loadDataSources() {
  // Deprecated: 数据源固定为 TradingView，UI 不再展示数据源下拉。
  // 保留函数以兼容旧代码引用（无操作）。
}

async function loadTimeframes() {
  try {
    const list = await API.get('/api/timeframes');
    const sel = $('#ds-timeframe');
    const cur = sel.dataset.current || '';
    sel.innerHTML = list.map(t => `<option value="${t}"${t === cur ? ' selected' : ''}>${t}</option>`).join('');
  } catch (e) {
    console.error('loadTimeframes:', e);
  }
}

// ── Events ─────────────────────────────────────────────────────────────
let currentAnalysisStream = null;

function bindEvents() {
  $('#btn-refresh').addEventListener('click', loadBars);
  // 合并分析按钮：根据当前状态决定是「开始分析」还是「取消分析」
  const btnAnalyzeToggle = $('#btn-analyze-toggle');
  if (btnAnalyzeToggle) {
    btnAnalyzeToggle.addEventListener('click', () => {
      const state = btnAnalyzeToggle.dataset.state || 'idle';
      if (state === 'idle') {
        startAnalysis();
      } else {
        // analyzing 或 loading 状态 → 取消
        if (currentAnalysisStream) {
          currentAnalysisStream.abort();
        }
        // 取消等待收盘倒计时（如处于等待中）
        stopWaitCloseCountdown();
      }
    });
  }
  // 增量分析按钮
  const btnIncremental = $('#btn-incremental');
  if (btnIncremental) {
    btnIncremental.addEventListener('click', () => {
      if (!btnIncremental.disabled) startIncrementalAnalysis();
    });
  }
  // 应用按钮：调用 /api/subscribe 切换交易所/品种/周期，然后刷新 K 线
  $('#btn-apply-subscribe').addEventListener('click', applySubscribe);

  // 历史按钮：切换 popover 显示，打开时刷新列表
  const btnHistory = $('#btn-history');
  if (btnHistory) {
    btnHistory.addEventListener('click', (e) => {
      e.stopPropagation();
      const pop = $('#history-popover');
      pop.classList.toggle('hidden');
      if (!pop.classList.contains('hidden')) loadHistoryList();
    });
  }
  // 历史刷新按钮
  const btnHistRefresh = $('#btn-history-refresh');
  if (btnHistRefresh) {
    btnHistRefresh.addEventListener('click', (e) => {
      e.stopPropagation();
      loadHistoryList();
    });
  }
  // 返回实时按钮：清除回看状态，隐藏 badge，切回 stream tab
  const btnBack = $('#btn-back-to-live');
  if (btnBack) {
    btnBack.addEventListener('click', () => {
      hideReplayBadge();
      lastRecord = null;
      $$('.sidebar-tabs .tab').forEach(b => b.classList.remove('active'));
      document.querySelector('.sidebar-tabs .tab[data-tab="stream"]')?.classList.add('active');
      $$('.tab-panel').forEach(p => p.classList.remove('active'));
      $('#tab-stream').classList.add('active');
    });
  }
  // 点击 popover 外部关闭它
  document.addEventListener('click', (e) => {
    const pop = $('#history-popover');
    if (pop && !pop.classList.contains('hidden')) {
      if (!pop.contains(e.target) && e.target.id !== 'btn-history') {
        pop.classList.add('hidden');
      }
    }
  });
  // 交易所切换：重新拉对应的品种列表
  $('#ds-exchange').addEventListener('change', () => {
    loadSymbols();
    // 不自动应用，等用户点"应用"按钮
  });
  // 品种下拉选择 → 同步到隐藏的 text input（供 applySubscribe 读取）
  const symSelect = $('#ds-symbol-select');
  if (symSelect) {
    symSelect.addEventListener('change', () => {
      $('#ds-symbol').value = symSelect.value;
    });
  }
  // 品种下拉/自定义输入切换
  const symToggleBtn = $('#btn-symbol-toggle');
  if (symToggleBtn) {
    symToggleBtn.addEventListener('click', toggleSymbolInputMode);
  }
  // 设置按钮
  $('#btn-settings').addEventListener('click', () => $('#settings-modal').classList.remove('hidden'));
  $('.modal-close').addEventListener('click', () => $('#settings-modal').classList.add('hidden'));
  $('#btn-modal-close').addEventListener('click', () => $('#settings-modal').classList.add('hidden'));
  $('#settings-modal').addEventListener('click', (e) => {
    if (e.target === $('#settings-modal')) $('#settings-modal').classList.add('hidden');
  });

  // 侧边栏折叠/展开
  const btnCollapse = $('#btn-sidebar-collapse');
  if (btnCollapse) btnCollapse.addEventListener('click', () => setSidebarCollapsed(true));
  const btnToggle = $('#btn-sidebar-toggle');
  if (btnToggle) btnToggle.addEventListener('click', () => {
    const sb = $('#sidebar');
    setSidebarCollapsed(!sb.classList.contains('collapsed'));
  });
  const fabExpand = $('#sidebar-expand-fab');
  if (fabExpand) fabExpand.addEventListener('click', () => setSidebarCollapsed(false));

  // 指标设置按钮
  const btnInd = $('#btn-indicators');
  if (btnInd) btnInd.addEventListener('click', openIndicatorsModal);
  const btnIndClose = $('#btn-indicators-close');
  if (btnIndClose) btnIndClose.addEventListener('click', () => $('#indicators-modal').classList.add('hidden'));
  const indModal = $('#indicators-modal');
  if (indModal) {
    indModal.addEventListener('click', (e) => {
      if (e.target === indModal) indModal.classList.add('hidden');
    });
    indModal.querySelector('.modal-close')?.addEventListener('click', () => indModal.classList.add('hidden'));
  }

  // Settings form
  $('#settings-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    await saveSettingsHandler();
  });

  // 飞书测试发送按钮
  $('#btn-feishu-test').addEventListener('click', feishuTestHandler);

  // Settings sub-tabs
  $$('.stab').forEach(btn => {
    btn.addEventListener('click', () => {
      $$('.stab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      $$('.stab-panel').forEach(p => p.classList.remove('active'));
      $(`#stab-${btn.dataset.stab}`).classList.add('active');
    });
  });

  // Sidebar tabs
  $$('.sidebar-tabs .tab').forEach(btn => {
    btn.addEventListener('click', () => {
      $$('.sidebar-tabs .tab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      $$('.tab-panel').forEach(p => p.classList.remove('active'));
      $(`#tab-${btn.dataset.tab}`).classList.add('active');
      // 「原始」「调试」tab 切换时按需重渲染，保证回看历史记录时数据是最新的
      const tab = btn.dataset.tab;
      if (tab === 'raw' && lastRecord) renderRaw(lastRecord);
      else if (tab === 'debug' && lastRecord) renderDebug(lastRecord);
      // Phase D Task 4 SubTask 4.10：切到「决策树可视化」tab 时按需重渲染
      if (tab === 'tree-viz' && lastRecord) {
        renderTreeViz(lastRecord);
      }
      // Phase A Task 1.3：决策 / 决策树 / 未来 tab 切回时重新渲染，避免显示陈旧内容
      if (tab === 'decision' && lastRecord && typeof renderDecision === 'function') {
        renderDecision(lastRecord);
      } else if (tab === 'tree' && lastRecord && typeof renderDecisionTree === 'function') {
        renderDecisionTree(lastRecord);
      } else if (tab === 'future' && lastRecord && typeof renderFuturePanel === 'function') {
        renderFuturePanel(lastRecord);
      }
    });
  });

  // Phase D Task 4 SubTask 4.9：决策树可视化播放控制按钮
  const treeVizPlayBtn = $('#btn-tree-viz-play');
  const treeVizPauseBtn = $('#btn-tree-viz-pause');
  const treeVizResetBtn = $('#btn-tree-viz-reset');
  if (treeVizPlayBtn) treeVizPlayBtn.addEventListener('click', () => {
    if (lastRecord?.decision_tree) {
      const duration = Number(currentSettings?.general?.decision_flow_play_seconds || 50);
      playPathAnimation(lastRecord.decision_tree, duration);
    }
  });
  if (treeVizPauseBtn) treeVizPauseBtn.addEventListener('click', stopPathAnimation);
  if (treeVizResetBtn) treeVizResetBtn.addEventListener('click', resetPathAnimation);

  // SVG 缩放/平移控件初始化（Ctrl+滚轮缩放、拖拽平移、按钮缩放）
  _initTreeVizZoomOnce();

  // Phase E1 Task 5 SubTask 5.3：原始 tab 轮次按钮点击切换
  document.querySelectorAll('.raw-turn-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.raw-turn-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      rawCurrentTurn = btn.dataset.turn;
      if (lastRecord) renderRaw(lastRecord);
    });
  });

  // Chat（追问嵌入实时 tab，Phase C Task 3）
  $('#btn-chat-send').addEventListener('click', sendChat);
  $('#chat-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') sendChat();
  });
  const btnChatClear = $('#btn-chat-clear');
  if (btnChatClear) btnChatClear.addEventListener('click', clearChatOutput);

  // 实时刷新开关：优先 SSE，失败降级为 3s 轮询
  $('#cb-live-refresh').addEventListener('change', (e) => {
    if (e.target.checked) {
      startSSEBarsStream();
    } else {
      stopSSEBarsStream();
      stopLiveRefresh();
      updateSSEStatus('off');
    }
  });

  // 等待收盘复选框：取消勾选时清掉倒计时
  const cbWaitClose = $('#cb-wait-close');
  if (cbWaitClose) {
    cbWaitClose.addEventListener('change', (e) => {
      if (!e.target.checked) {
        stopWaitCloseCountdown();
      }
    });
  }

  // 持续跟踪开关：状态变化时更新状态栏文本
  const cbKeepAnalysis = $('#cb-keep-analysis');
  if (cbKeepAnalysis) {
    cbKeepAnalysis.addEventListener('change', () => {
      // SSE 活跃时由 updateSSEStatusWithExpiry 接管文案（含持续跟踪后缀），
      // 否则交给 updateLiveRefreshStatus
      if (sseBarsStream && !sseFallbackPolling) {
        updateSSEStatusWithExpiry();
      } else {
        updateLiveRefreshStatus();
      }
    });
  }

  // API Key 警告「点击设置」链接：打开 settings modal + 聚焦 API Key 输入
  const apiKeyAlertOpen = $('#api-key-alert-open');
  if (apiKeyAlertOpen) {
    apiKeyAlertOpen.addEventListener('click', (e) => {
      e.preventDefault();
      $('#settings-modal').classList.remove('hidden');
      // 切到 AI 服务 tab（确保 API Key 输入框可见）
      $$('.stab').forEach(b => b.classList.remove('active'));
      const providerTab = document.querySelector('.stab[data-stab="s-provider"]');
      if (providerTab) providerTab.classList.add('active');
      $$('.stab-panel').forEach(p => p.classList.remove('active'));
      $('#stab-s-provider')?.classList.add('active');
      const apiKeyInput = $('#s-api-key');
      if (apiKeyInput) {
        apiKeyInput.focus();
        apiKeyInput.select();
      }
    });
  }

  // 品种输入框：用户修改内容时隐藏品种名校验警告
  const dsSymbol = $('#ds-symbol');
  if (dsSymbol) {
    dsSymbol.addEventListener('input', () => {
      const alert = $('#symbol-alert');
      if (alert) {
        alert.setAttribute('hidden', '');
        alert.textContent = '';
      }
    });
  }
  const dsSymbolSelect = $('#ds-symbol-select');
  if (dsSymbolSelect) {
    dsSymbolSelect.addEventListener('change', () => {
      const alert = $('#symbol-alert');
      if (alert) {
        alert.setAttribute('hidden', '');
        alert.textContent = '';
      }
    });
  }

  // 「原始」tab 按钮事件委托：复制调试信息 / 导出 JSON
  // （renderRaw 每次重渲染会替换 innerHTML，所以用事件委托而非直接绑定）
  const rawContent = $('#raw-content');
  if (rawContent) {
    rawContent.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-action]');
      if (!btn) return;
      const action = btn.dataset.action;
      if (action === 'copy-debug') copyDebugInfo();
      else if (action === 'export-json') exportRecordJson();
    });
  }

  // 页面可见性优化：隐藏时暂停 SSE/轮询，可见时恢复
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      // 页面隐藏 → 暂停 SSE 与轮询，降低后台资源占用
      if ($('#cb-live-refresh').checked) {
        stopSSEBarsStream();
        stopLiveRefresh();
        updateSSEStatus('paused');
      }
    } else {
      // 页面恢复可见 → 立即拉一次填补空缺，然后重启 SSE
      if ($('#cb-live-refresh').checked) {
        refreshBarsOnly();
        startSSEBarsStream();
      }
    }
  });
}

async function saveSettingsHandler() {
  try {
    const newTz = ($('#s-display-timezone')?.value || '').trim() || 'Asia/Shanghai';
    await API.put('/api/settings', {
      provider: {
        base_url: $('#s-base-url').value,
        model: $('#s-model').value,
        api_key: $('#s-api-key').value,
        reasoning_effort: $('#s-reasoning-effort').value,
        thinking: $('#s-thinking').checked,
        // Phase I Task 19: context_window 在 AIProviderSettings
        context_window: parseInt($('#s-context-window').value) || 2000000,
      },
      general: {
        refresh_interval_ms: parseInt($('#s-refresh-ms').value) || 1000,
        decision_stance: $('#s-decision-stance').value,
        context_warning_threshold_pct: parseInt($('#s-ctx-warn').value) || 80,
        analysis_bar_count: parseInt($('#ds-bar-count').value) || 100,
        display_timezone: newTz,
        // Phase I Task 19: 新增字段
        auto_resume_chart_after_analysis: $('#s-auto-resume-chart').checked,
        incremental_max_new_bars: parseInt($('#s-incremental-max-new-bars').value) || 10,
        keep_analysis: $('#s-keep-analysis').checked,
        cancel_keep_analysis_on_retry: $('#s-cancel-keep-on-retry').checked,
        enable_next_bar_prediction: $('#s-predict-next-bar').checked,
        decision_confidence_threshold: parseInt($('#s-decision-confidence-threshold').value) || 40,
        alert_on_order_opportunity: $('#s-alert-on-order-opportunity').checked,
        stream_pane_font_pt: parseInt($('#s-stream-font-size').value) || 11,
        chart_seq_label_font_pt: parseInt($('#s-chart-seq-font-size').value) || 11,
        decision_flow_auto_play: $('#s-decision-tree-autoplay').checked,
        decision_flow_play_seconds: parseInt($('#s-decision-tree-play-duration').value) || 50,
        decision_flow_default_zoom_pct: parseInt($('#s-decision-tree-default-zoom').value) || 600,
      },
      feishu: {
        enabled: $('#s-feishu-enabled').checked,
        webhook_url: $('#s-feishu-webhook').value,
        secret: $('#s-feishu-secret').value,
        app_id: $('#s-feishu-app-id').value,
        app_secret: $('#s-feishu-app-secret').value,
        notify_on_order_only: $('#s-feishu-order-only').checked,
      },
      // Phase I Task 19: 第三方
      pushplus: {
        token: $('#s-pushplus-token').value,
        enabled: $('#s-pushplus-enabled').checked,
      },
      tushare: {
        token: $('#s-tushare-token').value,
      },
    });
    $('#settings-modal').classList.add('hidden');
    alert('设置已保存');
    // 立即应用新时区到图表（不必等 loadSettings）
    displayTimezone = newTz;
    if (window._chartAPI?.setDisplayTimezone) {
      window._chartAPI.setDisplayTimezone(newTz);
      // 触发 lightweight-charts 重绘时间轴刻度
      try { chart?.applyOptions({}); } catch (_) {}
    }
    updateTimezoneLabel(newTz);
    // 重新加载以同步 currentSettings 与 context_window
    await loadSettings();
    // 若实时刷新已开启，重启 SSE 以应用新的设置（如 fallback polling 间隔）
    if ($('#cb-live-refresh').checked) {
      stopSSEBarsStream();
      startSSEBarsStream();
    }
  } catch (e) {
    alert('保存失败: ' + e.message);
  }
}

// ── 时区标签更新 ──────────────────────────────────────────────────────
// 根据当前 display_timezone 更新 #tz-label 文案。
// 空或无效时区 → "⏰ 浏览器本地时区"
function updateTimezoneLabel(tz) {
  const label = document.getElementById('tz-label');
  if (!label) return;
  if (!tz) {
    label.textContent = '⏰ 浏览器本地时区';
    return;
  }
  try {
    // 取 UTC 偏移短表达（如 GMT+8）
    const formatter = new Intl.DateTimeFormat('en-US', { timeZone: tz, timeZoneName: 'shortOffset' });
    const parts = formatter.formatToParts(new Date());
    const offsetPart = parts.find(p => p.type === 'timeZoneName')?.value || '';
    // 城市名取 IANA tz 最后一段，下划线转空格
    const city = tz.split('/').pop().replace(/_/g, ' ');
    label.textContent = `⏰ ${offsetPart} ${city}`;
  } catch (e) {
    label.textContent = '⏰ 浏览器本地时区';
  }
}

// 飞书测试发送
async function feishuTestHandler() {
  const result = $('#feishu-test-result');
  const btn = $('#btn-feishu-test');
  const webhook = $('#s-feishu-webhook').value.trim();
  const secret = $('#s-feishu-secret').value.trim();
  if (!webhook) {
    result.textContent = '请先填写 Webhook URL';
    result.className = 'muted-text status-warn';
    return;
  }
  btn.disabled = true;
  result.textContent = '发送中…';
  result.className = 'muted-text';
  try {
    const resp = await API.post('/api/feishu/test', { webhook_url: webhook, secret });
    result.textContent = '✓ 测试消息已发送';
    result.className = 'muted-text status-ok';
  } catch (e) {
    result.textContent = '✗ ' + (e.message || '失败');
    result.className = 'muted-text status-err';
  } finally {
    btn.disabled = false;
  }
}

// ── 应用订阅（切换交易所/品种/周期） ───────────────────────────────────
async function applySubscribe() {
  const btn = $('#btn-apply-subscribe');
  const symbol = $('#ds-symbol').value.trim().toUpperCase();
  const timeframe = $('#ds-timeframe').value;
  const exchange = $('#ds-exchange').value;
  if (!symbol) {
    alert('请输入品种代码');
    return;
  }
  // 切换前若实时刷新开启，先关闭 SSE，避免收到旧 symbol 的事件
  const wasLiveRefreshOn = $('#cb-live-refresh').checked;
  if (wasLiveRefreshOn) stopSSEBarsStream();
  btn.disabled = true;
  btn.textContent = '切换中…';
  // 先清空所有指标老数据，避免新数据来之前老指标还显示在图上
  if (window._indicatorsAPI) window._indicatorsAPI.clearAllData();
  try {
    await API.post('/api/subscribe', {
      kind: 'tradingview',
      symbol,
      timeframe,
      exchange,
    });
    // 切换成功后重新拉 K 线（loadBars 内部会通知指标库重新计算）
    await loadBars();
    // 切换 symbol/timeframe 后，历史记录列表也要刷新
    loadHistoryList();
    // 切换品种/周期时自动取消持续跟踪
    const cbKeep = $('#cb-keep-analysis');
    if (cbKeep) cbKeep.checked = false;
    updateLiveRefreshStatus();
    btn.textContent = '应用';
    btn.disabled = false;
    // 之前开启了实时刷新 → 为新 symbol 重启 SSE
    if (wasLiveRefreshOn) startSSEBarsStream();
    // 切换成功 → 检查是否有成功记录以决定增量按钮可用性
    refreshIncrementalButtonState();
  } catch (e) {
    const msg = e.message || String(e);
    // 品种名无效：错误信息含 symbol / not found / 404 时显示红色校验警告
    if (/symbol|not\s*found|404/i.test(msg)) {
      const alert = $('#symbol-alert');
      if (alert) {
        alert.textContent = '⚠️ 品种名无效，请检查';
        alert.removeAttribute('hidden');
      }
    }
    alert('切换失败: ' + msg);
    btn.textContent = '应用';
    btn.disabled = false;
    // 切换失败（后端仍为旧 symbol）→ 恢复 SSE 推送
    if (wasLiveRefreshOn) startSSEBarsStream();
  }
}

// ── 增量分析按钮可用性检查 ────────────────────────────────────────────
// 调 GET /api/records?exchange=&symbol=&timeframe=&limit=1 检查是否存在成功
// 记录。无成功记录 → 禁用按钮 + tooltip 提示；有 → 启用按钮。
async function refreshIncrementalButtonState() {
  const btn = $('#btn-incremental');
  if (!btn) return;
  try {
    const exchange = $('#ds-exchange').value
      || currentSettings?.general?.last_tradingview_exchange || '';
    const symbol = $('#ds-symbol').value
      || currentSettings?.general?.last_symbol || '';
    const timeframe = $('#ds-timeframe').value
      || currentSettings?.general?.last_timeframe || '';
    if (!exchange || !symbol || !timeframe) {
      btn.disabled = true;
      btn.title = '缺少交易所/品种/周期信息';
      return;
    }
    const data = await API.get(
      `/api/records?exchange=${encodeURIComponent(exchange)}&symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&limit=1`
    );
    if (Array.isArray(data) && data.length > 0 && !data[0].has_exception) {
      btn.disabled = false;
      btn.title = '基于上次成功记录做增量分析';
    } else {
      btn.disabled = true;
      btn.title = '无可用历史记录，请使用完整分析';
    }
  } catch (e) {
    btn.disabled = true;
    btn.title = '无可用历史记录，请使用完整分析';
  }
}

// ── 实时刷新（fallback 轮询；SSE 优先） ──────────────────────────────
// intervalMs 可选：指定轮询间隔（ms）；不传则读取设置中的 refresh_interval_ms
function startLiveRefresh(intervalMs) {
  stopLiveRefresh();
  const interval = intervalMs || (parseInt($('#s-refresh-ms').value) || 1000);
  // 限制最小 500ms，避免压垮后端
  const safeInterval = Math.max(500, interval);
  liveRefreshTimer = setInterval(refreshBarsOnly, safeInterval);
  liveRefreshLastTs = Date.now();
  updateLiveRefreshStatus();
}

// ── 品种输入模式切换（下拉 ↔ 自定义输入） ────────────────────────────
let symbolInputMode = 'select';  // 'select' or 'custom'

function toggleSymbolInputMode() {
  const sel = $('#ds-symbol-select');
  const input = $('#ds-symbol');
  const btn = $('#btn-symbol-toggle');
  if (symbolInputMode === 'select') {
    // 切换到自定义输入
    symbolInputMode = 'custom';
    sel.classList.add('hidden');
    input.classList.remove('hidden');
    input.value = sel.value || '';
    input.focus();
    btn.classList.add('active');
    btn.textContent = '📋';
    btn.title = '切换回下拉选择';
  } else {
    // 切换回下拉
    symbolInputMode = 'select';
    input.classList.add('hidden');
    sel.classList.remove('hidden');
    if (input.value) sel.value = input.value;  // 尝试选中
    btn.classList.remove('active');
    btn.textContent = '✏️';
    btn.title = '切换到自定义输入';
  }
}

// ── 指标管理（由 indicators.js 实现，app.js 只负责按钮绑定） ────────
// 旧版 EMA toggles 已迁移到 indicators.js 的统一指标管理器。
// 这里仅做 thin wrapper：按钮 → 调用 window._indicatorsAPI.openModal()
function openIndicatorsModal() {
  if (window._indicatorsAPI) window._indicatorsAPI.openModal();
}

function stopLiveRefresh() {
  if (liveRefreshTimer) {
    clearInterval(liveRefreshTimer);
    liveRefreshTimer = null;
  }
  updateLiveRefreshStatus();
}

function updateLiveRefreshStatus() {
  // 守卫：SSE 活跃（sseBarsStream 非 null 且非降级轮询）时由 updateSSEStatusWithExpiry
  // 接管状态栏文案，此处直接返回避免覆盖「距上次刷新 · 距下次收盘」文案
  if (sseBarsStream && !sseFallbackPolling) {
    return;
  }
  // SSE 模式（含降级轮询）下，状态文本由 updateSSEStatus 维护，此处跳过避免覆盖
  // 但仍需追加「持续跟踪中」标记，因此不能直接 return；改为若 SSE 活跃，仅
  // 在文本末尾追加持续跟踪标记。
  const cbKeep = $('#cb-keep-analysis');
  const keepSuffix = (cbKeep && cbKeep.checked) ? ' · 持续跟踪中' : '';
  if (sseBarsStream || sseFallbackPolling) {
    const el = $('#live-refresh-status');
    if (!el) return;
    // 保留 SSE 状态文案，仅追加持续跟踪后缀
    const base = el.textContent || '';
    const stripped = keepSuffix ? base.replace(/ · 持续跟踪中$/, '') : base;
    el.textContent = keepSuffix ? (stripped + keepSuffix) : stripped;
    return;
  }
  const el = $('#live-refresh-status');
  if (!el) return;
  if (!liveRefreshTimer) {
    el.textContent = keepSuffix ? keepSuffix.replace(/^ · /, '') : '';
    if (!el.textContent) el.style.color = '';
    return;
  }
  const elapsed = liveRefreshLastTs ? Math.max(0, Date.now() - liveRefreshLastTs) : 0;
  el.textContent = `· 距上次刷新 ${(elapsed / 1000).toFixed(1)}s${keepSuffix}`;
}

// 每秒更新一次"距上次刷新"显示
setInterval(updateLiveRefreshStatus, 1000);

// ── SSE 实时 K 线流（/api/bars/stream） ───────────────────────────────
// 优先使用 SSE 推送；连接失败时降级为 3s 轮询；页面不可见时暂停。
function startSSEBarsStream() {
  // 先关闭已有 SSE 连接与 fallback 轮询
  stopSSEBarsStream();
  try {
    sseBarsStream = new EventSource('/api/bars/stream');

    // bar_close 事件：一根 K 线收盘，推送完整 bars 数组
    sseBarsStream.addEventListener('bar_close', (e) => {
      try {
        const data = JSON.parse(e.data);
        const bars = data.bars || [];
        if (bars.length) {
          lastBars = bars;
          setBars(candleSeries, bars);
          setSeqMarkers(candleSeries, bars);
          if (window._indicatorsAPI) window._indicatorsAPI.onBarsUpdated(bars);
          // 更新最新一根 bar 的时间锚点
          const sorted = [...bars].sort((a, b) => a.ts_open - b.ts_open);
          if (sorted.length) window.__PA_LAST_BAR_TIME__ = sorted[sorted.length - 1].ts_open / 1000;
        }
        // 解析后端附带的 next_close_ts（新 forming bar 的收盘时间戳，ms）
        if (data.next_close_ts != null && data.next_close_ts > 0) {
          sseNextCloseTs = Number(data.next_close_ts);
        } else {
          sseNextCloseTs = 0;
        }
        sseLastBarUpdateTs = Date.now();
        liveRefreshLastTs = sseLastBarUpdateTs;
        // SSE 恢复正常 → 清除 fallback 标记并停止轮询
        if (sseFallbackPolling) {
          sseFallbackPolling = false;
          stopLiveRefresh();
        }
        // 启动每秒更新状态栏（含 elapsed/remaining）
        // 注意：必须在收到 bar_close 后启动，而非 onopen，否则 sseNextCloseTs 还没更新
        startSSEStatusExpiryTimer();
        // SSE 活跃时由 updateSSEStatusWithExpiry 接管文案（含 elapsed/remaining）
        updateSSEStatusWithExpiry();
        // 持续跟踪：K 线收盘后自动触发新一轮分析（分析期间不重复触发）
        const cbKeep = $('#cb-keep-analysis');
        if (cbKeep && cbKeep.checked && !isAnalyzing) {
          startAnalysis();
        }
      } catch (err) {
        console.error('bar_close event error:', err);
      }
    });

    // bar_update 事件：每 5s 推送正在形成的最后一根 bar
    sseBarsStream.addEventListener('bar_update', (e) => {
      try {
        const data = JSON.parse(e.data);
        const bar = data.last_bar;
        if (bar) {
          // 仅更新最后一根（forming）bar
          candleSeries.update({
            time: bar.ts_open / 1000,
            open: bar.open,
            high: bar.high,
            low: bar.low,
            close: bar.close,
          });
          // 同步 lastBars 末尾元素
          if (lastBars && lastBars.length) {
            const lastIdx = lastBars.length - 1;
            if (lastBars[lastIdx].ts_open === bar.ts_open) {
              lastBars[lastIdx] = bar;
            }
          }
        }
        // 解析后端附带的 next_close_ts（当前 forming bar 的收盘时间戳，ms）
        if (data.next_close_ts != null && data.next_close_ts > 0) {
          sseNextCloseTs = Number(data.next_close_ts);
        } else {
          sseNextCloseTs = 0;
        }
        sseLastBarUpdateTs = Date.now();
        liveRefreshLastTs = sseLastBarUpdateTs;
        if (sseFallbackPolling) {
          sseFallbackPolling = false;
          stopLiveRefresh();
        }
        // 启动每秒更新状态栏（含 elapsed/remaining）
        // 注意：必须在收到 bar_update 后启动，而非 onopen，否则 sseNextCloseTs 还没更新
        startSSEStatusExpiryTimer();
        updateSSEStatusWithExpiry();
      } catch (err) {
        console.error('bar_update event error:', err);
      }
    });

    // ping 事件：心跳，连接活跃
    sseBarsStream.addEventListener('ping', () => {
      liveRefreshLastTs = Date.now();
      // SSE 活跃期间，仅刷新 elapsed；不更新 next_close_ts
      if (sseLastBarUpdateTs) updateSSEStatusWithExpiry();
      else updateLiveRefreshStatus();
    });

    sseBarsStream.onopen = () => {
      console.log('SSE bars stream connected');
      // SSE 连接成功 → 启动每秒更新状态栏
      // 注意：此处不设 sseLastBarUpdateTs 和 sseNextCloseTs
      //   - sseLastBarUpdateTs: 等收到第一个 bar_update/bar_close 事件再设
      //   - sseNextCloseTs: 同上，避免残留旧值导致「距下次收盘」显示异常
      // 启动定时器后状态栏仅显示「● SSE 实时」，等首个事件到达再补 elapsed/remaining
      startSSEStatusExpiryTimer();
      updateSSEStatus('ok');
      if (sseFallbackPolling) {
        sseFallbackPolling = false;
        stopLiveRefresh();
        updateSSEStatus('ok');
      }
    };

    sseBarsStream.onerror = () => {
      console.warn('SSE bars stream error, falling back to polling');
      // 关闭已损坏的连接
      if (sseBarsStream) {
        sseBarsStream.close();
        sseBarsStream = null;
      }
      // SSE 断开 → 停止每秒更新状态栏，恢复 fallback 文案
      stopSSEStatusExpiryTimer();
      sseNextCloseTs = 0;
      sseLastBarUpdateTs = 0;
      // 启动 fallback 轮询（仅当尚未降级时，避免重复启动）
      if (!sseFallbackPolling) {
        sseFallbackPolling = true;
        startLiveRefresh(3000);  // 3s 间隔，降低后端压力
        updateSSEStatus('fallback');
      }
      // 10s 后尝试重连 SSE（仅在用户仍开启实时且页面可见时）
      if (sseReconnectTimer) clearTimeout(sseReconnectTimer);
      sseReconnectTimer = setTimeout(() => {
        if ($('#cb-live-refresh').checked && !document.hidden) {
          startSSEBarsStream();
        }
      }, 10000);
    };

    updateSSEStatus('connecting');
  } catch (e) {
    console.error('Failed to start SSE bars stream:', e);
    // SSE 构造失败 → 立即降级为轮询
    sseFallbackPolling = true;
    startLiveRefresh(3000);
    updateSSEStatus('fallback');
  }
}

function stopSSEBarsStream() {
  if (sseBarsStream) {
    sseBarsStream.close();
    sseBarsStream = null;
  }
  if (sseReconnectTimer) {
    clearTimeout(sseReconnectTimer);
    sseReconnectTimer = null;
  }
  if (sseFallbackPolling) {
    stopLiveRefresh();
    sseFallbackPolling = false;
  }
  // 清除每秒更新状态栏的定时器并重置 SSE 状态变量
  stopSSEStatusExpiryTimer();
  sseNextCloseTs = 0;
  sseLastBarUpdateTs = 0;
}

// 启动「距上次刷新 · 距下次收盘」每秒更新定时器（幂等）
function startSSEStatusExpiryTimer() {
  if (sseStatusExpiryTimer) return;
  sseStatusExpiryTimer = setInterval(updateSSEStatusWithExpiry, 1000);
}

// 停止每秒更新状态栏定时器（幂等）
function stopSSEStatusExpiryTimer() {
  if (sseStatusExpiryTimer) {
    clearInterval(sseStatusExpiryTimer);
    sseStatusExpiryTimer = null;
  }
}

// timeframe 字符串 → 秒数（与后端 bar_close_wait.timeframe_to_seconds 对齐）
// 用于 SSE 状态栏倒计时 sanity check，防止 next_close_ts 过期/竞态时显示异常值
function timeframeToSeconds(tf) {
  const t = String(tf || '').trim();
  if (!t) return 0;
  const m = t.match(/^(\d+)([mhdw])$/i);
  if (!m) return 0;
  const n = parseInt(m[1], 10);
  const unit = m[2].toLowerCase();
  if (unit === 'm') return n * 60;
  if (unit === 'h') return n * 3600;
  if (unit === 'd') return n * 86400;
  if (unit === 'w') return n * 7 * 86400;
  return 0;
}

// 计算并更新 #live-refresh-status 文案：● SSE 实时 · 距上次刷新 Ns · 距下次收盘 Ms
// 仅在 SSE 活跃（sseBarsStream 非 null 且非 fallback）时由定时器调用
function updateSSEStatusWithExpiry() {
  // 守卫：SSE 不活跃或降级轮询时直接 return（由 updateLiveRefreshStatus 接管）
  if (!sseBarsStream || sseFallbackPolling) return;
  const el = $('#live-refresh-status');
  if (!el) return;
  const elapsed = sseLastBarUpdateTs ? Math.max(0, (Date.now() - sseLastBarUpdateTs) / 1000) : 0;
  // sanity check：remaining 不能超过当前 timeframe 的 duration
  // 防止 next_close_ts 过期、时区偏移或跨周期残留导致显示异常大的倒计时
  const tfSecs = timeframeToSeconds(currentSettings?.general?.last_timeframe || '');
  let remaining = 0;
  let closeText = '';
  if (sseNextCloseTs > 0 && tfSecs > 0) {
    remaining = Math.max(0, (sseNextCloseTs - Date.now()) / 1000);
    // 若 remaining 超过 1 个 timeframe duration，说明 next_close_ts 已过期或错误，丢弃
    if (remaining > tfSecs) {
      console.warn('[SSE] next_close_ts sanity check failed:', {
        sseNextCloseTs, tfSecs, remaining,
        now: Date.now()
      });
    } else {
      closeText = ` · 距下次收盘 ${remaining.toFixed(0)}s`;
    }
  }
  let text = `● SSE 实时 · 距上次刷新 ${elapsed.toFixed(1)}s${closeText}`;
  // 持续跟踪后缀
  const cbKeep = $('#cb-keep-analysis');
  if (cbKeep && cbKeep.checked) text += ' · 持续跟踪中';
  el.textContent = text;
  el.style.color = '#26a69a';
}

// 更新 SSE 状态指示器（#live-refresh-status）
// state: 'ok' | 'connecting' | 'fallback' | 'paused' | 'off'
function updateSSEStatus(state) {
  const el = $('#live-refresh-status');
  if (!el) return;
  let base = '';
  switch (state) {
    case 'ok':
      base = '● SSE 实时';
      el.style.color = '#26a69a';
      break;
    case 'connecting':
      base = '○ 连接中...';
      el.style.color = '#ffc800';
      break;
    case 'fallback':
      base = '⚠ 实时连接断开，已降级轮询';
      el.style.color = '#ef5350';
      break;
    case 'paused':
      base = '⏸ 已暂停（页面不可见）';
      el.style.color = '#787b86';
      break;
    case 'off':
    default:
      base = '';
      el.style.color = '';
      break;
  }
  // 追加持续跟踪标记
  const cbKeep = $('#cb-keep-analysis');
  if (cbKeep && cbKeep.checked && base) {
    base = base + ' · 持续跟踪中';
  }
  el.textContent = base;
}

// ── Analysis ───────────────────────────────────────────────────────────

// 分析按钮状态机：idle → analyzing → idle
//   idle: 蓝色 ▶ 分析（点击开始）
//   analyzing: 红色 ⏹ 取消（脉动动画，点击中止）
function setAnalyzeButtonState(state) {
  const btn = $('#btn-analyze-toggle');
  if (!btn) return;
  btn.dataset.state = state;
  const iconEl = btn.querySelector('.btn-analyze-icon');
  const textEl = btn.querySelector('.btn-analyze-text');
  if (state === 'idle') {
    btn.disabled = false;
    btn.title = '开始 AI 分析';
    if (iconEl) iconEl.textContent = '▶';
    if (textEl) textEl.textContent = '分析';
  } else if (state === 'analyzing') {
    btn.disabled = false;  // 仍可点击用于取消
    btn.title = '点击取消当前分析';
    if (iconEl) iconEl.textContent = '⏹';
    if (textEl) textEl.textContent = '取消';
  }
}

// ── FlowBar 5 步进度条（Phase J Task 20） ─────────────────────────────
// 根据 SSE 事件实时切换 6 个 step 的 active/done 状态：
//   1=等待数据 / 2=阶段一推理 / 3=阶段一验证 / 4=阶段二推理 / 5=阶段二验证 / 6=完成
function setFlowBarStep(step) {
  const bar = $('#flow-bar');
  if (!bar) return;
  bar.querySelectorAll('.flow-step').forEach(el => {
    const s = parseInt(el.dataset.step);
    el.classList.remove('active', 'done');
    if (s < step) el.classList.add('done');
    else if (s === step) el.classList.add('active');
  });
}

function showFlowBar() {
  const bar = $('#flow-bar');
  if (bar) bar.hidden = false;
  setFlowBarStep(1);
}

function hideFlowBar() {
  const bar = $('#flow-bar');
  if (bar) bar.hidden = true;
}

// 侧边栏折叠/展开
function setSidebarCollapsed(collapsed) {
  const sb = $('#sidebar');
  const fab = $('#sidebar-expand-fab');
  if (!sb) return;
  if (collapsed) {
    sb.classList.add('collapsed');
    if (fab) fab.classList.add('visible');
  } else {
    sb.classList.remove('collapsed');
    if (fab) fab.classList.remove('visible');
  }
  // 折叠/展开后图表宽度变化，需要重新调整尺寸
  setTimeout(() => {
    resizeChart();
    // 同步副图宽度
    if (window._indicatorsState) {
      for (const inst of window._indicatorsState.activeIndicators) {
        if (inst._subChart) {
          try {
            const el = $('#chart-container');
            const parentWidth = (el.parentElement || el).clientWidth;
            inst._subChart.applyOptions({ width: parentWidth });
          } catch (_) {}
        }
      }
    }
  }, 280);  // 等待 CSS transition 完成（0.25s）
}

async function startAnalysis() {
  // 等待收盘：若勾选了「等待收盘」复选框，先调 /api/bars/next-close 拿到剩余
  // 秒数，每秒更新倒计时，归零后再实际发起 /api/analyze/stream 请求。
  const cbWaitClose = $('#cb-wait-close');
  if (cbWaitClose && cbWaitClose.checked) {
    const started = await startWaitCloseCountdown();
    if (!started) {
      // 用户在等待期间取消勾选，或后端无法获取剩余秒数 → 不发分析请求
      return;
    }
  }

  setAnalyzeButtonState('analyzing');
  isAnalyzing = true;
  showFlowBar();

  // Phase A Task 2.3：清除历史回看 banner，避免与新流式输出混淆
  document.querySelectorAll('#tab-stream .replay-banner').forEach(el => el.remove());

  // Clear panels — 清空 stage1/stage2 的 prompt 与 answer，重置 status
  resetStageBlock(1);
  resetStageBlock(2);
  $('#stream-usage').textContent = '';
  $('#stage-badge').textContent = '';
  $('#decision-content').innerHTML = '';
  $('#future-content').innerHTML = '分析中…';
  $('#tree-content').innerHTML = '分析中…';
  // 重置 token 进度条
  updateTokenProgress(null);
  // 清空图表叠加层（保留 EMA 与 K 线）
  clearOverlays(candleSeries);
  setSeqMarkers(candleSeries, lastBars || []);

  const barCount = parseInt($('#ds-bar-count').value) || 100;

  try {
    const { controller, source } = API.sse(`/api/analyze/stream?bar_count=${barCount}`);
    currentAnalysisStream = controller;

    let stage = '';
    let currentStage = 1;  // 当前 token 应写入哪个阶段（1 或 2）
    for await (const evt of source) {
      switch (evt.type) {
        case 'orchestrator_event':
          // 处理 orchestrator 全部 11 个事件
          switch (evt.event) {
            case 'Stage1Started':
              stage = '🔍 阶段一：市场诊断';
              currentStage = 1;
              setStageStatus(1, '进行中…', 'active');
              setFlowBarStep(2);
              break;
            case 'Stage1Retry':
              stage = evt.attempt != null
                ? `🔄 阶段一第 ${evt.attempt} 次重试…`
                : '🔄 阶段一重试中…';
              if (evt.reason) stage += `（${evt.reason}）`;
              setStageStatus(1, `重试中（第 ${evt.attempt || '?'} 次）`, 'active');
              break;
            case 'Stage1Done':
              stage = '⏳ 构建阶段二…';
              setStageStatus(1, '✓ 完成', 'done');
              setFlowBarStep(3);
              break;
            case 'Stage1Failed':
              stage = '❌ 阶段一失败';
              if (evt.reason) stage += `：${evt.reason}`;
              setStageStatus(1, '✗ 失败' + (evt.reason ? `：${evt.reason}` : ''), 'failed');
              break;
            case 'Stage2Started':
              stage = '🎯 阶段二：交易决策';
              currentStage = 2;
              setStageStatus(2, '进行中…', 'active');
              setFlowBarStep(4);
              break;
            case 'Stage2Retry':
              stage = evt.attempt != null
                ? `🔄 阶段二第 ${evt.attempt} 次重试…`
                : '🔄 阶段二重试中…';
              if (evt.reason) stage += `（${evt.reason}）`;
              setStageStatus(2, `重试中（第 ${evt.attempt || '?'} 次）`, 'active');
              break;
            case 'Stage2Done':
              stage = '✅ 分析完成';
              setStageStatus(2, '✓ 完成', 'done');
              setFlowBarStep(5);
              break;
            case 'Stage2Failed':
              stage = '❌ 阶段二失败';
              if (evt.reason) stage += `：${evt.reason}`;
              setStageStatus(2, '✗ 失败' + (evt.reason ? `：${evt.reason}` : ''), 'failed');
              break;
            case 'InsufficientData':
              stage = '⚠️ 数据不足，无法分析';
              if (evt.reason) stage += `：${evt.reason}`;
              break;
            case 'RecordSaved':
              appendStageContent(currentStage, '\n💾 记录已保存');
              break;
            case 'Cancelled':
              stage = '⚠️ 已取消';
              break;
            default:
              break;
          }
          $('#stage-badge').textContent = stage;
          break;
        case 'reasoning_token':
          appendStageReasoning(currentStage, evt.chunk);
          break;
        case 'content_token':
          appendStageContent(currentStage, evt.chunk);
          break;
        case 'stage_prompt':
          setStagePrompt(evt.stage, evt.system, evt.user);
          break;
        case 'strategy_files':
          // 可选：展示命中的策略文件
          if (Array.isArray(evt.files) && evt.files.length) {
            appendStageContent(currentStage, `\n📑 策略文件：${evt.files.join(', ')}`);
          }
          break;
        case 'done': {
          lastRecord = evt.record;
          renderDecision(evt.record);
          renderFuturePanel(evt.record);
          renderDecisionTree(evt.record);
          renderRaw(evt.record);
          renderDebug(evt.record);
          renderTokenUsage(evt.record.usage_total);
          updateTokenProgress(evt.record.usage_total);
          // Phase D Task 4：决策树可视化 tab 同步渲染（仅在 tab 可见时才有视觉效果，
          // 但仍调用以更新内部状态，便于切换 tab 时立即显示）
          renderTreeViz(evt.record);
          // 新分析完成 → 刷新历史列表（新记录应出现在顶部）
          loadHistoryList();
          // 图表叠加层
          const overlay = evt.record.decision_overlay || evt.record.stage2_decision || {};
          setDecisionOverlays(candleSeries, overlay);
          setDirectionMarker(candleSeries, overlay);
          const s1 = evt.record.stage1_diagnosis || {};
          const srLevels = extractSupportResistance(s1);
          if (srLevels.length) {
            // 注意：setSupportResistance 会清掉 decision price lines，因此先画 SR 再画 decision
            setSupportResistance(candleSeries, srLevels);
            setDecisionOverlays(candleSeries, overlay);
          }
          fitView(chart, 20, lastBars ? lastBars.length : 0);
          enableChat();
          // 分析完成后刷新增量分析按钮可用性（新记录可被增量复用）
          refreshIncrementalButtonState();
          // 下单机会提醒（Phase E Task 12）：Toast + 浏览器通知 + 蜂鸣音
          // 触发条件：order_type ∈ [limit, market, stop] 且 trade_confidence >= 阈值
          // 受 settings.general.alert_on_order_opportunity 开关控制（默认 true，!== false 才触发）
          triggerOrderAlertIfNeeded(evt.record);
          // FlowBar：标记完成步，5 秒后隐藏进度条
          setFlowBarStep(6);
          setTimeout(hideFlowBar, 5000);
          break;
        }
        case 'error':
          $('#stage-badge').textContent = `❌ 错误: ${evt.message}`;
          appendStageContent(currentStage, `\n[错误] ${evt.message}`);
          break;
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') {
      $('#stage-badge').textContent = `❌ 连接错误: ${e.message}`;
    }
  } finally {
    setAnalyzeButtonState('idle');
    currentAnalysisStream = null;
    isAnalyzing = false;
  }
}

// ── 等待 K 线收盘倒计时 ────────────────────────────────────────────────
// 调 GET /api/bars/next-close 拿 seconds_remaining，setInterval 每秒递减更新
// #wait-close-countdown 文案「等待收盘：还剩 Ns」；归零后 clearInterval 并
// 返回 true 表示可以继续发起分析。用户中途取消勾选则返回 false。
async function startWaitCloseCountdown() {
  // 先清掉旧 timer（如有）
  stopWaitCloseCountdown();
  const span = $('#wait-close-countdown');
  if (!span) return false;
  let remaining = 0;
  try {
    const symbol = $('#ds-symbol').value || currentSettings?.general?.last_symbol || '';
    const timeframe = $('#ds-timeframe').value || currentSettings?.general?.last_timeframe || '';
    const exchange = $('#ds-exchange').value || currentSettings?.general?.last_tradingview_exchange || '';
    const data = await API.get(
      `/api/bars/next-close?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&exchange=${encodeURIComponent(exchange)}`
    );
    remaining = data.seconds_remaining;
    if (remaining == null || remaining <= 0) {
      // 已收盘或无法计算 → 直接继续分析
      return true;
    }
  } catch (e) {
    console.error('startWaitCloseCountdown:', e);
    // 后端调用失败 → 不阻塞分析，直接继续
    return true;
  }
  // 禁用分析按钮，避免在等待期间重复提交
  const btnAnalyze = $('#btn-analyze-toggle');
  if (btnAnalyze) btnAnalyze.disabled = true;
  span.textContent = `等待收盘：还剩 ${remaining}s`;
  span.removeAttribute('hidden');
  return await new Promise((resolve) => {
    waitCloseCountdownTimer = setInterval(() => {
      // 用户取消勾选 → 中止等待
      const cb = $('#cb-wait-close');
      if (cb && !cb.checked) {
        stopWaitCloseCountdown();
        resolve(false);
        return;
      }
      remaining -= 1;
      if (remaining <= 0) {
        stopWaitCloseCountdown();
        resolve(true);
        return;
      }
      span.textContent = `等待收盘：还剩 ${remaining}s`;
    }, 1000);
  });
}

function stopWaitCloseCountdown() {
  if (waitCloseCountdownTimer) {
    clearInterval(waitCloseCountdownTimer);
    waitCloseCountdownTimer = null;
  }
  const span = $('#wait-close-countdown');
  if (span) {
    span.setAttribute('hidden', '');
    span.textContent = '';
  }
  const btnAnalyze = $('#btn-analyze-toggle');
  if (btnAnalyze) btnAnalyze.disabled = false;
}

// ── 增量分析（基于上次成功记录） ──────────────────────────────────────
// 复用 startAnalysis 的事件处理逻辑，仅切换 endpoint 为 /api/analyze/incremental/stream
async function startIncrementalAnalysis() {
  const cbWaitClose = $('#cb-wait-close');
  if (cbWaitClose && cbWaitClose.checked) {
    const started = await startWaitCloseCountdown();
    if (!started) return;
  }

  setAnalyzeButtonState('analyzing');
  isAnalyzing = true;
  showFlowBar();

  resetStageBlock(1);
  resetStageBlock(2);
  $('#stream-usage').textContent = '';
  $('#stage-badge').textContent = '🔄 增量分析中…';
  $('#decision-content').innerHTML = '';
  $('#future-content').innerHTML = '分析中…';
  $('#tree-content').innerHTML = '分析中…';
  updateTokenProgress(null);
  clearOverlays(candleSeries);
  setSeqMarkers(candleSeries, lastBars || []);

  const barCount = parseInt($('#ds-bar-count').value) || 100;

  try {
    const { controller, source } = API.sse(`/api/analyze/incremental/stream?bar_count=${barCount}`);
    currentAnalysisStream = controller;

    let stage = '';
    let currentStage = 1;
    for await (const evt of source) {
      switch (evt.type) {
        case 'orchestrator_event':
          switch (evt.event) {
            case 'Stage1Started':
              stage = '🔍 阶段一：市场诊断（增量）';
              currentStage = 1;
              setStageStatus(1, '进行中…', 'active');
              setFlowBarStep(2);
              break;
            case 'Stage1Retry':
              stage = `🔄 阶段一第 ${evt.attempt || '?'} 次重试…`;
              setStageStatus(1, `重试中（第 ${evt.attempt || '?'} 次）`, 'active');
              break;
            case 'Stage1Done':
              stage = '⏳ 构建阶段二…';
              setStageStatus(1, '✓ 完成', 'done');
              setFlowBarStep(3);
              break;
            case 'Stage1Failed':
              stage = '❌ 阶段一失败';
              if (evt.reason) stage += `：${evt.reason}`;
              setStageStatus(1, '✗ 失败' + (evt.reason ? `：${evt.reason}` : ''), 'failed');
              break;
            case 'Stage2Started':
              stage = '🎯 阶段二：交易决策';
              currentStage = 2;
              setStageStatus(2, '进行中…', 'active');
              setFlowBarStep(4);
              break;
            case 'Stage2Retry':
              stage = `🔄 阶段二第 ${evt.attempt || '?'} 次重试…`;
              setStageStatus(2, `重试中（第 ${evt.attempt || '?'} 次）`, 'active');
              break;
            case 'Stage2Done':
              stage = '✅ 增量分析完成';
              setStageStatus(2, '✓ 完成', 'done');
              setFlowBarStep(5);
              break;
            case 'Stage2Failed':
              stage = '❌ 阶段二失败';
              if (evt.reason) stage += `：${evt.reason}`;
              setStageStatus(2, '✗ 失败' + (evt.reason ? `：${evt.reason}` : ''), 'failed');
              break;
            case 'InsufficientData':
              stage = '⚠️ 数据不足，无法分析';
              if (evt.reason) stage += `：${evt.reason}`;
              break;
            case 'RecordSaved':
              appendStageContent(currentStage, '\n💾 记录已保存');
              break;
            case 'Cancelled':
              stage = '⚠️ 已取消';
              break;
            default:
              break;
          }
          $('#stage-badge').textContent = stage;
          break;
        case 'reasoning_token':
          appendStageReasoning(currentStage, evt.chunk);
          break;
        case 'content_token':
          appendStageContent(currentStage, evt.chunk);
          break;
        case 'stage_prompt':
          setStagePrompt(evt.stage, evt.system, evt.user);
          break;
        case 'strategy_files':
          if (Array.isArray(evt.files) && evt.files.length) {
            appendStageContent(currentStage, `\n📑 策略文件：${evt.files.join(', ')}`);
          }
          break;
        case 'done': {
          lastRecord = evt.record;
          renderDecision(evt.record);
          renderFuturePanel(evt.record);
          renderDecisionTree(evt.record);
          renderRaw(evt.record);
          renderDebug(evt.record);
          renderTokenUsage(evt.record.usage_total);
          updateTokenProgress(evt.record.usage_total);
          // Phase D Task 4：决策树可视化 tab 同步渲染
          renderTreeViz(evt.record);
          loadHistoryList();
          const overlay = evt.record.decision_overlay || evt.record.stage2_decision || {};
          setDecisionOverlays(candleSeries, overlay);
          setDirectionMarker(candleSeries, overlay);
          const s1 = evt.record.stage1_diagnosis || {};
          const srLevels = extractSupportResistance(s1);
          if (srLevels.length) {
            setSupportResistance(candleSeries, srLevels);
            setDecisionOverlays(candleSeries, overlay);
          }
          fitView(chart, 20, lastBars ? lastBars.length : 0);
          enableChat();
          refreshIncrementalButtonState();
          // 下单机会提醒（Phase E Task 12）
          triggerOrderAlertIfNeeded(evt.record);
          // FlowBar：标记完成步，5 秒后隐藏进度条
          setFlowBarStep(6);
          setTimeout(hideFlowBar, 5000);
          break;
        }
        case 'error':
          $('#stage-badge').textContent = `❌ 错误: ${evt.message}`;
          appendStageContent(currentStage, `\n[错误] ${evt.message}`);
          break;
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') {
      $('#stage-badge').textContent = `❌ 连接错误: ${e.message}`;
    }
  } finally {
    setAnalyzeButtonState('idle');
    currentAnalysisStream = null;
    isAnalyzing = false;
  }
}

// ── Stage block 辅助函数 ────────────────────────────────────────────────
function resetStageBlock(n) {
  setStageStatus(n, '', '');
  $(`#stage${n}-system`).textContent = '';
  $(`#stage${n}-user`).textContent = '';
  $(`#stage${n}-reasoning`).textContent = '';
  $(`#stage${n}-content`).textContent = '';
  // 重新展开折叠块，确保新一轮分析的输出可见
  const block = $(`#stage-${n}-block`);
  if (block) block.querySelectorAll('details').forEach(d => { d.open = true; });
}

function setStageStatus(n, text, cls) {
  const el = $(`#stage${n}-status`);
  if (!el) return;
  el.textContent = text;
  el.classList.remove('active', 'done', 'failed');
  if (cls) el.classList.add(cls);
}

function setStagePrompt(stage, system, user) {
  // stage 可能是 'stage1' / 'stage2' / 1 / 2
  const n = String(stage).endsWith('2') || stage === 2 ? 2 : 1;
  if (system) $(`#stage${n}-system`).textContent = system;
  if (user) $(`#stage${n}-user`).textContent = user;
}

function appendStageReasoning(n, chunk) {
  const el = $(`#stage${n}-reasoning`);
  if (!el) return;
  el.textContent += chunk;
  el.scrollTop = el.scrollHeight;
  const stage = n === 1 ? 'stage1' : 'stage2';
  stageCharCounts[stage].reasoning += (chunk || '').length;
  updateStreamStats();
}

function appendStageContent(n, chunk) {
  const el = $(`#stage${n}-content`);
  if (!el) return;
  el.textContent += chunk;
  el.scrollTop = el.scrollHeight;
  const stage = n === 1 ? 'stage1' : 'stage2';
  stageCharCounts[stage].content += (chunk || '').length;
  updateStreamStats();
}

// ── 决策卡片渲染 ──────────────────────────────────────────────────────
// ── 决策面板：分区标题 + 字段栅格 helper ─────────────────────────────

// 渲染分区标题（左侧色条 + 标题 + 分隔线）
function renderSectionHeading(title, color = '#2962ff') {
  return `<div class="section-heading" style="border-left-color: ${color}"><span class="section-heading-title">${title}</span></div>`;
}

// 置信度阈值变色：>=70 绿 / 50-69 黄 / <50 红
function confidenceColor(value) {
  if (value == null) return '#787b86';
  const v = Number(value);
  if (isNaN(v)) return '#787b86';
  if (v >= 70) return '#26a69a';
  if (v >= 50) return '#ffc800';
  return '#ef5350';
}

// 短字段栅格：fields = [[key, valHtml, title?], ...]
function fieldGrid(fields) {
  if (!fields || !fields.length) return '';
  const items = fields.map(([k, v, t]) => {
    const titleAttr = t ? ` title="${escapeHtml(t)}"` : '';
    return `<div class="field"${titleAttr}><span class="field-key">${escapeHtml(k)}</span><span class="field-val">${v}</span></div>`;
  }).join('');
  return `<div class="field-grid">${items}</div>`;
}

// 全宽长文本字段
function fieldFull(key, valHtml, title) {
  const titleAttr = title ? ` title="${escapeHtml(title)}"` : '';
  return `<div class="field-full"${titleAttr}><div class="field-key">${escapeHtml(key)}</div><div class="field-val">${valHtml}</div></div>`;
}

// 进度条字段（百分比 0-100，按阈值变色）
function fieldBar(key, value, title) {
  const v = parsePercent(value);
  const titleAttr = title ? ` title="${escapeHtml(title)}"` : '';
  return `<div class="field-bar"${titleAttr}>
    <div class="field-key"><span>${escapeHtml(key)}</span><span class="field-val">${v}%</span></div>
    <div class="bar-track"><div class="bar-fill" style="width: ${v}%; background: ${confidenceColor(v)};"></div></div>
  </div>`;
}

// 列表字段（chip 形式）
function fieldList(key, items, title) {
  if (!Array.isArray(items) || !items.length) return '';
  const titleAttr = title ? ` title="${escapeHtml(title)}"` : '';
  const chips = items.map(it => `<span class="chip">${escapeHtml(String(it))}</span>`).join('');
  return `<div class="field-list"${titleAttr}><div class="field-key">${escapeHtml(key)}</div><div class="field-val-list">${chips}</div></div>`;
}

// 置信度阈值过滤：trade_confidence < threshold 时强制改不下单 + reasoning 前缀 + 清空价格字段
function applyConfidenceThreshold(decision, threshold) {
  if (!decision || !threshold) return decision;
  const confidence = Number(decision.trade_confidence || 0);
  if (confidence >= threshold) return decision;
  // 置信度低于阈值，强制改不下单
  const modified = { ...decision };
  modified.order_type = '不下单';
  const prefix = `有入场机会，但置信度未通过（${confidence}/100 < 阈值 ${threshold}/100）\n\n`;
  modified.reasoning = prefix + (modified.reasoning || modified.brief_reasoning || '');
  // 清空订单价格字段
  modified.entry_price = null;
  modified.stop_loss_price = null;
  modified.take_profit_price = null;
  modified.take_profit_price_2 = null;
  return modified;
}

// 趋势/方向颜色编码：bullish/震荡偏多→绿；bearish/震荡偏空→红；neutral/震荡→黄
function trendColor(direction) {
  if (!direction) return '';
  const d = String(direction).toLowerCase();
  if (d === 'bullish' || d === '上涨' || d === '震荡偏多') return '#26a69a';
  if (d === 'bearish' || d === '下跌' || d === '震荡偏空') return '#ef5350';
  if (d === 'neutral' || d === '震荡') return '#ffc800';
  return '';
}

// 盈亏比/交易员方程通过状态颜色 + 标签
function rrPassColor(rr, passed) {
  if (passed === true || passed === 'true') return { color: '#26a69a', label: '方程通过' };
  if (passed === false || passed === 'false') return { color: '#ef5350', label: '方程不通过' };
  return { color: '', label: '' };
}

// ── 决策 tab 重设计（frontend-design methodology） ───────────────
// 视觉论题：外科手术级决策报告 — 三区域视觉层次
//   VERDICT  (决策结论) - 蓝色 - 常显 - "做什么"：结论横幅 + 价格 + 盈亏比 + 三置信度条
//   VITALS   (市场状态) - 青色 - 常显 - "为什么"：趋势结构 / 市场阶段 / 关键价位 / 形态信号
//   EVIDENCE (详细依据) - 紫色 - 折叠 - "证据链"：7 个独立子折叠区 + 全部展开/折叠主控
// 内容计划：每个区域有清晰的子分组，子分组有小标题；缺失字段（支撑/阻力/置信度）已补全
// 交互论题：Section 3 每个子区域独立 <details> + 主控按钮；默认仅 3.1 决策理由展开
function renderDecision(record) {
  const threshold = Number(currentSettings?.general?.decision_confidence_threshold || 40);
  const d = applyConfidenceThreshold(record?.stage2_decision || {}, threshold);
  const s1 = record.stage1_diagnosis || {};
  const orderType = d.order_type || '不下单';
  const direction = d.order_direction || '';
  const cls = direction === '做多' || direction === 'buy' || direction === 'long' ? 'buy' :
              direction === '做空' || direction === 'sell' || direction === 'short' ? 'sell' : '';
  const isNoOrder = orderType === '不下单' || orderType === 'no_order';
  const stance = isNoOrder ? '观望' : '入场';

  let html = '<div class="disclaimer">⚠️ 分析仅供参考，不构成投资建议</div>';
  html += _renderVerdictSection(d, s1, orderType, direction, cls, isNoOrder, stance);
  html += _renderVitalsSection(s1);
  html += _renderEvidenceSection(d, s1);

  $('#decision-content').innerHTML = html;
  _initDecisionToggleAll();
}

// ── Section 1: VERDICT 决策结论 ───────────────────────────────────
function _renderVerdictSection(d, s1, orderType, direction, cls, isNoOrder, stance) {
  let html = `<div class="decis-section decis-verdict ${cls}">`;
  html += `<div class="decis-section-head">
    <span class="decis-section-icon">🎯</span>
    <span class="decis-section-title">决策结论</span>
    <span class="decis-section-tag">VERDICT</span>
  </div>`;
  html += `<div class="decis-section-body">`;

  // 1.1 结论横幅：order_type + 方向 + 交易置信度
  const dirColor = trendColor(direction);
  const dirStyle = dirColor ? ` style="color: ${dirColor}"` : '';
  const tc = parsePercent(d.trade_confidence);
  const tcc = confidenceColor(tc);
  const confInline = d.trade_confidence != null
    ? `<span class="decis-conf-inline" style="color:${tcc}">置信度 ${tc}/100 · ${stance}</span>` : '';
  html += `<div class="decis-banner">
    <span class="decis-order-type">${escapeHtml(bilingual(orderType, ORDER_TYPE_ZH))}</span>
    ${direction ? `<span class="decis-direction"${dirStyle}>${escapeHtml(bilingual(direction, DIRECTION_ZH))}</span>` : ''}
    ${confInline}
  </div>`;

  // 1.2 派生字段：趋势 / 周期 / 阶段
  const derivedFields = [];
  if (s1 && (s1.direction || s1.cycle_position)) {
    const trendLabel = formatTrendLabel(s1.direction, s1.cycle_position);
    const trendCol = trendLabelColor(trendLabel);
    const trendStyle = trendCol ? ` style="color: ${trendCol}"` : '';
    derivedFields.push(['趋势', `<span${trendStyle}>${escapeHtml(trendLabel)}</span>`, 'direction + cycle_position 派生（对齐 GUI format_trend_label）']);
  }
  if (s1 && s1.cycle_position) {
    const cycleLabel = formatCycleWithDirection(s1.cycle_position, s1.direction);
    const altCycle = s1.alternative_cycle_position ? `<span class="alt-cycle">（备选 ${escapeHtml(bilingualCycle(s1.alternative_cycle_position))}）</span>` : '';
    derivedFields.push(['周期', `${escapeHtml(cycleLabel)}${altCycle}`, 'cycle_position + 方向 + 备选（对齐 GUI format_cycle_with_direction）']);
  }
  if (s1 && s1.market_phase) {
    const phaseLabel = bilingual(s1.market_phase, MARKET_PHASE_ZH);
    const riskSuffix = s1.transition_risk ? ` · 风险 ${RISK_LEVEL_ZH[(s1.transition_risk || '').toLowerCase()] || s1.transition_risk}` : '';
    derivedFields.push(['阶段', `${escapeHtml(phaseLabel)}${escapeHtml(riskSuffix)}`, 'market_phase + transition_risk']);
  }
  if (derivedFields.length) html += fieldGrid(derivedFields);

  // 1.3 价格栅格 + 盈亏比（不下单时隐藏）
  if (!isNoOrder) {
    const priceFields = [];
    if (d.entry_price != null) priceFields.push(['入场价', escapeHtml(String(d.entry_price)), 'entry_price']);
    if (d.stop_loss_price != null) priceFields.push(['止损', escapeHtml(String(d.stop_loss_price)), 'stop_loss_price']);
    if (d.take_profit_price != null) priceFields.push(['止盈 TP1', escapeHtml(String(d.take_profit_price)), 'take_profit_price']);
    if (d.take_profit_price_2 != null) priceFields.push(['止盈 TP2', escapeHtml(String(d.take_profit_price_2)), 'take_profit_price_2']);
    if (priceFields.length) html += fieldGrid(priceFields);

    const rr = computeRiskReward(d.entry_price, d.take_profit_price, d.stop_loss_price, direction);
    if (rr) {
      const winRate = parseWinRate(d.estimated_win_rate);
      let passes = null;
      if (winRate != null && rr.risk > 0 && rr.reward > 0) {
        passes = (winRate / 100) * rr.reward >= ((100 - winRate) / 100) * rr.risk;
      }
      const rrInfo = rrPassColor(rr, passes);
      const eqNote = passes !== null ? ` · ${rrInfo.label}` : '';
      const rrInlineText = `${rr.ratio.toFixed(2)}:1（风险 ${rr.risk.toFixed(2)} / 回报 ${rr.reward.toFixed(2)}）${eqNote}`;
      const rrStyle = rrInfo.color ? ` style="color: ${rrInfo.color}; font-weight: 600;"` : '';
      html += fieldGrid([['盈亏比', `<span${rrStyle}>${escapeHtml(rrInlineText)}</span>`, 'reward:risk（交易员方程，对齐 GUI compute_risk_reward）']]);
    }
  }

  // 1.4 三置信度条：诊断置信度 / 交易决策置信度 / 预估胜率
  if (d.diagnosis_confidence != null) {
    html += fieldBar('诊断置信度', d.diagnosis_confidence, 'diagnosis_confidence：阶段二对市场诊断的置信度（0-100）');
  }
  if (d.trade_confidence != null) {
    const tcc2 = confidenceColor(tc);
    html += `<div class="field-bar" title="trade_confidence：本次交易下单的置信度（0-100）">
      <div class="field-key"><span>交易决策置信度</span><span class="field-val" style="color: ${tcc2}; font-weight: 600;">${tc}/100 · ${stance}</span></div>
      <div class="bar-track"><div class="bar-fill" style="width: ${tc}%; background: ${tcc2};"></div></div>
    </div>`;
  }
  if (d.estimated_win_rate != null) {
    html += fieldBar('预估胜率', d.estimated_win_rate, 'estimated_win_rate：预估胜率（0-100）');
  }

  html += `</div></div>`;
  return html;
}

// ── Section 2: VITALS 市场状态 ────────────────────────────────────
function _renderVitalsSection(s1) {
  if (!s1) return '';
  const hasData = s1.direction || s1.cycle_position || s1.market_phase || s1.volatility_regime ||
                   s1.spike_stage || (s1.climax_risk && s1.climax_risk !== 'none') ||
                   (Array.isArray(s1.support_levels) && s1.support_levels.length) ||
                   (Array.isArray(s1.resistance_levels) && s1.resistance_levels.length) ||
                   (Array.isArray(s1.key_signals) && s1.key_signals.length) ||
                   (Array.isArray(s1.detected_patterns) && s1.detected_patterns.length);
  if (!hasData) return '';

  let html = `<div class="decis-section decis-vitals">`;
  html += `<div class="decis-section-head">
    <span class="decis-section-icon">🔬</span>
    <span class="decis-section-title">市场状态</span>
    <span class="decis-section-tag">VITALS</span>
  </div>`;
  html += `<div class="decis-section-body">`;

  // 2.1 趋势结构：方向 + 周期位置 + 备选周期 + 派生趋势标签
  const trendFields = [];
  if (s1.direction) {
    const s1DirColor = trendColor(s1.direction);
    const s1DirStyle = s1DirColor ? ` style="color: ${s1DirColor}"` : '';
    trendFields.push(['方向', `<span${s1DirStyle}>${escapeHtml(bilingual(s1.direction, DIRECTION_ZH))}</span>`, 'direction：阶段一判定方向']);
  }
  if (s1.cycle_position) {
    trendFields.push(['周期位置', escapeHtml(bilingualCycle(s1.cycle_position)), 'cycle_position']);
  }
  if (s1.alternative_cycle_position) {
    trendFields.push(['备选周期', escapeHtml(bilingualCycle(s1.alternative_cycle_position)), 'alternative_cycle_position']);
  }
  if (s1.direction && s1.cycle_position) {
    const trendLabel = formatTrendLabel(s1.direction, s1.cycle_position);
    const trendCol = trendLabelColor(trendLabel);
    const trendStyle = trendCol ? ` style="color: ${trendCol}"` : '';
    trendFields.push(['趋势标签', `<span${trendStyle}>${escapeHtml(trendLabel)}</span>`, 'direction + cycle 派生']);
  }
  if (trendFields.length) html += _renderVitalsSubsection('趋势结构', trendFields);

  // 2.2 市场阶段：market_phase + transition_risk + volatility_regime + spike_stage + climax_risk
  const phaseFields = [];
  if (s1.market_phase) {
    phaseFields.push(['市场阶段', escapeHtml(bilingual(s1.market_phase, MARKET_PHASE_ZH)), 'market_phase']);
  }
  if (s1.transition_risk) {
    const riskZh = RISK_LEVEL_ZH[(s1.transition_risk || '').toLowerCase()] || s1.transition_risk;
    phaseFields.push(['过渡风险', escapeHtml(riskZh), 'transition_risk：过渡风险等级']);
  }
  if (s1.volatility_regime) {
    const volZh = { low: '低', medium: '中', high: '高', extreme: '极高' }[String(s1.volatility_regime).toLowerCase()] || s1.volatility_regime;
    phaseFields.push(['波动率', escapeHtml(volZh), 'volatility_regime：波动率分级']);
  }
  if (s1.spike_stage) {
    const spikeZh = { active: '活跃', ending: '结束中', transitioning: '过渡中' }[String(s1.spike_stage).toLowerCase()] || s1.spike_stage;
    phaseFields.push(['Spike 阶段', escapeHtml(spikeZh), 'spike_stage：尖峰阶段']);
  }
  if (s1.climax_risk && s1.climax_risk !== 'none') {
    const climaxZh = { warning: '警告', triggered: '已触发' }[String(s1.climax_risk).toLowerCase()] || s1.climax_risk;
    phaseFields.push(['高潮风险', escapeHtml(climaxZh), 'climax_risk：高潮风险等级']);
  }
  if (phaseFields.length) html += _renderVitalsSubsection('市场阶段', phaseFields);

  // 2.3 关键价位：支撑位（绿）/ 阻力位（红）并排
  const hasSupport = Array.isArray(s1.support_levels) && s1.support_levels.length;
  const hasResistance = Array.isArray(s1.resistance_levels) && s1.resistance_levels.length;
  if (hasSupport || hasResistance) {
    html += `<div class="decis-subsection">`;
    html += `<div class="decis-subsection-head"><span class="decis-dot"></span>关键价位</div>`;
    html += `<div class="decis-subsection-body">`;
    html += `<div class="sr-pair" title="support_levels / resistance_levels：阶段一识别的关键价位">`;
    html += `<div class="sr-col sr-support">
      <div class="sr-col-label">支撑位</div>
      <div class="sr-col-chips">${hasSupport ? s1.support_levels.map(v => `<span class="chip chip-support">${escapeHtml(String(v))}</span>`).join('') : '<span class="sr-empty">—</span>'}</div>
    </div>`;
    html += `<div class="sr-col sr-resistance">
      <div class="sr-col-label">阻力位</div>
      <div class="sr-col-chips">${hasResistance ? s1.resistance_levels.map(v => `<span class="chip chip-resistance">${escapeHtml(String(v))}</span>`).join('') : '<span class="sr-empty">—</span>'}</div>
    </div>`;
    html += `</div></div></div>`;
  }

  // 2.4 形态与信号：detected_patterns + key_signals
  const hasPatterns = Array.isArray(s1.detected_patterns) && s1.detected_patterns.length;
  const hasSignals = Array.isArray(s1.key_signals) && s1.key_signals.length;
  if (hasPatterns || hasSignals) {
    html += `<div class="decis-subsection">`;
    html += `<div class="decis-subsection-head"><span class="decis-dot"></span>形态与信号</div>`;
    html += `<div class="decis-subsection-body">`;
    if (hasPatterns) html += fieldList('识别形态', s1.detected_patterns, 'detected_patterns：阶段一识别到的形态列表');
    if (hasSignals) html += fieldList('关键信号', s1.key_signals, 'key_signals：关键交易信号');
    html += `</div></div>`;
  }

  html += `</div></div>`;
  return html;
}

// VITALS 子分组渲染：小标题 + 字段栅格
function _renderVitalsSubsection(title, fields) {
  if (!fields || !fields.length) return '';
  return `<div class="decis-subsection">
    <div class="decis-subsection-head"><span class="decis-dot"></span>${escapeHtml(title)}</div>
    <div class="decis-subsection-body">${fieldGrid(fields)}</div>
  </div>`;
}

// ── Section 3: EVIDENCE 详细依据 ──────────────────────────────────
function _renderEvidenceSection(d, s1) {
  const subs = [];

  // 3.1 决策理由（默认展开）
  if (d.reasoning) {
    subs.push(['3.1 决策理由', [fieldFull('分析理由', escapeHtml(String(d.reasoning)), 'reasoning：本次决策的完整逻辑说明')], true]);
  }

  // 3.2 入场规则：entry_rule + entry_basis_bar + entry_basis_extreme + entry_setup
  const entryParts = [];
  if (d.entry_rule) entryParts.push(fieldFull('入场规则', escapeHtml(String(d.entry_rule)), 'entry_rule：入场触发规则'));
  if (d.entry_basis_bar != null) entryParts.push(fieldGrid([['入场基准K线', escapeHtml(String(d.entry_basis_bar)), 'entry_basis_bar']]));
  if (d.entry_basis_extreme != null) entryParts.push(fieldGrid([['入场基准极值', escapeHtml(String(d.entry_basis_extreme)), 'entry_basis_extreme']]));
  if (s1.entry_setup) entryParts.push(fieldFull('入场设置', escapeHtml(String(s1.entry_setup)), 'entry_setup：阶段一建议的入场设置'));
  if (entryParts.length) subs.push(['3.2 入场规则', entryParts, false]);

  // 3.3 K线分析：bar_analysis + bar_by_bar_summary
  const klineParts = [];
  if (s1.bar_analysis && typeof s1.bar_analysis === 'object' && Object.keys(s1.bar_analysis).length) {
    klineParts.push(_renderBarAnalysis(s1.bar_analysis));
  }
  if (Array.isArray(s1.bar_by_bar_summary) && s1.bar_by_bar_summary.length) {
    klineParts.push(_renderBarByBarSummaryInner(s1.bar_by_bar_summary));
  }
  if (klineParts.length) subs.push(['3.3 K线分析', klineParts, false]);

  // 3.4 趋势上下文：trend_context + htf_context
  const trendParts = [];
  if (s1.trend_context && typeof s1.trend_context === 'object' && Object.keys(s1.trend_context).length) {
    trendParts.push(_renderTrendContext(s1.trend_context));
  }
  if (s1.htf_context) {
    trendParts.push(fieldFull('HTF 背景', escapeHtml(String(s1.htf_context)), 'htf_context：高周期背景'));
  }
  if (trendParts.length) subs.push(['3.4 趋势上下文', trendParts, false]);

  // 3.5 风险评估：risk_assessment + invalidation_condition + risk_warning
  const riskParts = [];
  if (d.risk_assessment) riskParts.push(fieldFull('风险评估', escapeHtml(String(d.risk_assessment)), 'risk_assessment：本次交易风险评估'));
  if (d.invalidation_condition) riskParts.push(fieldFull('无效条件', escapeHtml(String(d.invalidation_condition)), 'invalidation_condition：交易失效条件'));
  if (s1.risk_warning) riskParts.push(fieldFull('风险警告', escapeHtml(String(s1.risk_warning)), 'risk_warning：阶段一风险警告'));
  if (riskParts.length) subs.push(['3.5 风险评估', riskParts, false]);

  // 3.6 关键因素与关注点：key_factors + watch_points
  const factorParts = [];
  if (Array.isArray(d.key_factors) && d.key_factors.length) {
    factorParts.push(fieldList('关键因素', d.key_factors, 'key_factors：影响本次决策的关键因素'));
  }
  if (Array.isArray(d.watch_points) && d.watch_points.length) {
    factorParts.push(fieldList('关注点', d.watch_points, 'watch_points：需要持续关注的要点'));
  }
  if (factorParts.length) subs.push(['3.6 关键因素与关注点', factorParts, false]);

  // 3.7 置信度说明：3 个 reasoning
  const confParts = [];
  if (d.diagnosis_confidence_reasoning) {
    confParts.push(fieldFull('诊断置信度说明', escapeHtml(String(d.diagnosis_confidence_reasoning)), 'diagnosis_confidence_reasoning'));
  }
  if (d.trade_confidence_reasoning) {
    confParts.push(fieldFull('交易置信度说明', escapeHtml(String(d.trade_confidence_reasoning)), 'trade_confidence_reasoning'));
  }
  if (d.estimated_win_rate_reasoning) {
    confParts.push(fieldFull('胜率说明', escapeHtml(String(d.estimated_win_rate_reasoning)), 'estimated_win_rate_reasoning'));
  }
  if (confParts.length) subs.push(['3.7 置信度说明', confParts, false]);

  if (!subs.length) return '';

  let html = `<div class="decis-section decis-evidence">`;
  html += `<div class="decis-section-head">
    <span class="decis-section-icon">📚</span>
    <span class="decis-section-title">详细依据</span>
    <span class="decis-section-tag">EVIDENCE · ${subs.length} 项</span>
    <button class="decis-toggle-all" data-action="expand-all" title="一键展开/折叠所有子区">全部展开</button>
  </div>`;
  html += `<div class="decis-section-body">`;
  subs.forEach(([title, parts, openByDefault]) => {
    html += `<details class="decis-sub-details"${openByDefault ? ' open' : ''}>
      <summary>${escapeHtml(title)}</summary>
      <div class="decis-sub-body">${parts.join('')}</div>
    </details>`;
  });
  html += `</div></div>`;
  return html;
}

// 主控按钮：全部展开 / 全部折叠
function _initDecisionToggleAll() {
  const btn = document.querySelector('.decis-toggle-all');
  if (!btn) return;
  const syncLabel = () => {
    const section = btn.closest('.decis-evidence');
    if (!section) return;
    const allDetails = section.querySelectorAll('details.decis-sub-details');
    if (!allDetails.length) return;
    const allOpen = Array.from(allDetails).every(d => d.open);
    btn.textContent = allOpen ? '全部折叠' : '全部展开';
    btn.dataset.action = allOpen ? 'collapse-all' : 'expand-all';
  };
  btn.addEventListener('click', () => {
    const section = btn.closest('.decis-evidence');
    if (!section) return;
    const allDetails = section.querySelectorAll('details.decis-sub-details');
    if (!allDetails.length) return;
    const allOpen = Array.from(allDetails).every(d => d.open);
    allDetails.forEach(d => { d.open = !allOpen; });
    syncLabel();
  });
  // 监听单个 details 切换，同步主控按钮文案
  document.querySelectorAll('details.decis-sub-details').forEach(d => {
    d.addEventListener('toggle', syncLabel);
  });
  syncLabel();
}

// 渲染 bar_by_bar_summary 内部内容（不包 decision-card，适配 EVIDENCE 子折叠区）
function _renderBarByBarSummaryInner(summary) {
  if (!Array.isArray(summary) || !summary.length) return '';
  const rows = summary.map((it, i) => {
    const bar = escapeHtml(String(it.bar || `#${i + 1}`));
    const role = escapeHtml(String(it.role || ''));
    const barType = escapeHtml(String(it.bar_type || ''));
    const head = `<span class="bar-summary-bar">${bar}</span><span class="bar-summary-role">${role}</span><span class="bar-summary-type">${barType}</span>`;
    const detailFields = [];
    if (it.bar != null && it.bar !== '') detailFields.push(['K线', escapeHtml(String(it.bar)), 'bar']);
    if (it.role != null && it.role !== '') detailFields.push(['角色', escapeHtml(String(it.role)), 'role']);
    if (it.bar_type != null && it.bar_type !== '') detailFields.push(['K线类型', escapeHtml(String(it.bar_type)), 'bar_type']);
    if (it.context_effect != null && it.context_effect !== '') detailFields.push(['上下文效应', escapeHtml(String(it.context_effect)), 'context_effect']);
    if (it.follow_through != null && it.follow_through !== '') detailFields.push(['跟随', escapeHtml(String(it.follow_through)), 'follow_through']);
    if (it.trapped_side != null && it.trapped_side !== '') detailFields.push(['被困方', escapeHtml(String(it.trapped_side)), 'trapped_side']);
    if (it.reason != null && it.reason !== '') detailFields.push(['原因', escapeHtml(String(it.reason)), 'reason']);
    return `<details class="bar-summary-row">
      <summary>${head}</summary>
      <div class="bar-summary-detail">${fieldGrid(detailFields)}</div>
    </details>`;
  }).join('');
  return `<details class="bar-summary-block" open>
    <summary>📜 逐棒摘要（${summary.length} 根）</summary>
    <div class="bar-summary-list">${rows}</div>
  </details>`;
}

// 渲染 trend_context 子字段网格（Stage1）
function _renderTrendContext(tc) {
  if (!tc || typeof tc !== 'object') return '';
  const fields = [];
  if (tc.background_direction != null && tc.background_direction !== '') {
    fields.push(['背景方向', escapeHtml(bilingual(tc.background_direction, DIRECTION_ZH)), 'background_direction：背景方向']);
  }
  if (tc.trading_direction != null && tc.trading_direction !== '') {
    fields.push(['交易方向', escapeHtml(bilingual(tc.trading_direction, DIRECTION_ZH)), 'trading_direction：交易方向']);
  }
  if (tc.primary_direction != null && tc.primary_direction !== '') {
    fields.push(['主方向', escapeHtml(bilingual(tc.primary_direction, DIRECTION_ZH)), 'primary_direction：主方向']);
  }
  if (tc.conflict != null) {
    fields.push(['冲突', tc.conflict ? '是' : '否', 'conflict：方向是否冲突']);
  }
  if (tc.relationship != null && tc.relationship !== '') {
    fields.push(['关系', escapeHtml(String(tc.relationship)), 'relationship：方向间关系']);
  }
  if (tc.recent_spike != null && tc.recent_spike !== '') {
    fields.push(['近期 Spike', escapeHtml(bilingual(tc.recent_spike, DIRECTION_ZH)), 'recent_spike：近期 spike 方向']);
  }
  if (tc.with_trend_rule != null && tc.with_trend_rule !== '') {
    fields.push(['顺势规则', escapeHtml(String(tc.with_trend_rule)), 'with_trend_rule：顺势规则']);
  }
  if (!fields.length) return '';
  return `<div class="subfield-block"><div class="subfield-title">趋势上下文</div>${fieldGrid(fields)}</div>`;
}

// 渲染 bar_analysis 卡片（Stage1 / Stage2 共用）
function _renderBarAnalysis(ba) {
  if (!ba || typeof ba !== 'object') return '';
  const fields = [];
  if (ba.always_in != null && ba.always_in !== '') {
    fields.push(['Always-In', escapeHtml(bilingual(ba.always_in, DIRECTION_ZH)), 'always_in：Always-In 方向']);
  }
  if (ba.last_closed_bar != null && ba.last_closed_bar !== '') {
    fields.push(['最近收盘K线', escapeHtml(String(ba.last_closed_bar)), 'last_closed_bar：最近收盘 K 线']);
  }
  if (ba.bar_type != null && ba.bar_type !== '') {
    fields.push(['K线类型', escapeHtml(String(ba.bar_type)), 'bar_type：K 线类型']);
  }
  if (ba.entry_setup_type != null && ba.entry_setup_type !== '') {
    fields.push(['入场设置类型', escapeHtml(String(ba.entry_setup_type)), 'entry_setup_type：入场设置类型']);
  }
  if (ba.follow_through != null && ba.follow_through !== '') {
    fields.push(['跟随', escapeHtml(String(ba.follow_through)), 'follow_through：跟随情况']);
  }
  if (ba.tr_position != null && ba.tr_position !== '') {
    fields.push(['TR 位置', escapeHtml(String(ba.tr_position)), 'tr_position：TR 位置']);
  }
  if (ba.breakout_quality != null && ba.breakout_quality !== '') {
    fields.push(['突破质量', escapeHtml(String(ba.breakout_quality)), 'breakout_quality：突破质量']);
  }

  let html = `<div class="bar-analysis-card">`;
  html += `<div class="bar-analysis-title">📊 当前 K 线分析</div>`;
  if (fields.length) {
    html += fieldGrid(fields);
  }
  // signal_bar 子对象
  if (ba.signal_bar && typeof ba.signal_bar === 'object' && Object.keys(ba.signal_bar).length) {
    const sb = ba.signal_bar;
    const sbFields = [];
    if (sb.bar != null && sb.bar !== '') sbFields.push(['K线', escapeHtml(String(sb.bar)), 'signal_bar.bar：信号 K 线']);
    if (sb.quality != null && sb.quality !== '') sbFields.push(['质量', escapeHtml(String(sb.quality)), 'signal_bar.quality：信号质量']);
    if (sb.pattern != null && sb.pattern !== '') sbFields.push(['形态', escapeHtml(String(sb.pattern)), 'signal_bar.pattern：信号形态']);
    if (sb.reason != null && sb.reason !== '') sbFields.push(['原因', escapeHtml(String(sb.reason)), 'signal_bar.reason：信号原因']);
    if (sbFields.length) {
      html += `<div class="subfield-block"><div class="subfield-title">信号K线</div>${fieldGrid(sbFields)}</div>`;
    }
  }
  // entry_bar 子对象
  if (ba.entry_bar && typeof ba.entry_bar === 'object' && Object.keys(ba.entry_bar).length) {
    const eb = ba.entry_bar;
    const ebFields = [];
    if (eb.bar != null && eb.bar !== '') ebFields.push(['K线', escapeHtml(String(eb.bar)), 'entry_bar.bar：入场 K 线']);
    if (eb.strength != null && eb.strength !== '') ebFields.push(['强度', escapeHtml(String(eb.strength)), 'entry_bar.strength：入场强度']);
    if (eb.follow_through != null && eb.follow_through !== '') ebFields.push(['跟随', escapeHtml(String(eb.follow_through)), 'entry_bar.follow_through：跟随情况']);
    if (eb.still_valid != null) ebFields.push(['仍有效', escapeHtml(String(eb.still_valid)), 'entry_bar.still_valid：是否仍有效']);
    if (eb.freshness != null && eb.freshness !== '') ebFields.push(['新鲜度', escapeHtml(String(eb.freshness)), 'entry_bar.freshness：新鲜度']);
    if (ebFields.length) {
      html += `<div class="subfield-block"><div class="subfield-title">入场K线</div>${fieldGrid(ebFields)}</div>`;
    }
  }
  // second_entry 子对象
  if (ba.second_entry && typeof ba.second_entry === 'object' && Object.keys(ba.second_entry).length) {
    const se = ba.second_entry;
    const seFields = [];
    if (se.is_second_entry != null) seFields.push(['是否二次入场', escapeHtml(String(se.is_second_entry)), 'second_entry.is_second_entry：是否为二次入场']);
    if (se.type != null && se.type !== '') seFields.push(['类型', escapeHtml(String(se.type)), 'second_entry.type：二次入场类型']);
    if (seFields.length) {
      html += `<div class="subfield-block"><div class="subfield-title">二次入场</div>${fieldGrid(seFields)}</div>`;
    }
  }
  html += `</div>`;
  return html;
}

// 渲染 bar_by_bar_summary（逐棒摘要，可折叠）
function _renderBarByBarSummary(summary) {
  if (!Array.isArray(summary) || !summary.length) return '';
  const rows = summary.map((it, i) => {
    const bar = escapeHtml(String(it.bar || `#${i + 1}`));
    const role = escapeHtml(String(it.role || ''));
    const barType = escapeHtml(String(it.bar_type || ''));
    const head = `<span class="bar-summary-bar">${bar}</span><span class="bar-summary-role">${role}</span><span class="bar-summary-type">${barType}</span>`;

    const detailFields = [];
    if (it.bar != null && it.bar !== '') detailFields.push(['K线', escapeHtml(String(it.bar)), 'bar：K 线标识']);
    if (it.role != null && it.role !== '') detailFields.push(['角色', escapeHtml(String(it.role)), 'role：K 线角色']);
    if (it.bar_type != null && it.bar_type !== '') detailFields.push(['K线类型', escapeHtml(String(it.bar_type)), 'bar_type：K 线类型']);
    if (it.context_effect != null && it.context_effect !== '') detailFields.push(['上下文效应', escapeHtml(String(it.context_effect)), 'context_effect：上下文效应']);
    if (it.follow_through != null && it.follow_through !== '') detailFields.push(['跟随', escapeHtml(String(it.follow_through)), 'follow_through：跟随情况']);
    if (it.trapped_side != null && it.trapped_side !== '') detailFields.push(['被困方', escapeHtml(String(it.trapped_side)), 'trapped_side：被困方']);
    if (it.reason != null && it.reason !== '') detailFields.push(['原因', escapeHtml(String(it.reason)), 'reason：原因']);

    return `<details class="bar-summary-row">
      <summary>${head}</summary>
      <div class="bar-summary-detail">${fieldGrid(detailFields)}</div>
    </details>`;
  }).join('');
  return `<div class="decision-card">
    <details class="bar-summary-block" open>
      <summary>📜 逐棒摘要</summary>
      <div class="bar-summary-list">${rows}</div>
    </details>
  </div>`;
}

// 渲染 node_overrides（AI 覆盖节点）
function _renderNodeOverrides(overrides, title) {
  if (!Array.isArray(overrides) || !overrides.length) return '';
  const items = overrides.map((it) => {
    const parts = [];
    if (it.node_id != null && it.node_id !== '') parts.push(`<span class="node-override-id">${escapeHtml(String(it.node_id))}</span>`);
    if (it.program_answer != null && it.program_answer !== '') parts.push(`<span class="node-override-program">程序: ${escapeHtml(String(it.program_answer))}</span>`);
    if (it.ai_answer != null && it.ai_answer !== '') parts.push(`<span class="node-override-ai">AI: ${escapeHtml(String(it.ai_answer))}</span>`);
    if (it.answer != null && it.answer !== '') parts.push(`<span class="node-override-answer">回答: ${escapeHtml(String(it.answer))}</span>`);
    if (it.branch != null && it.branch !== '') parts.push(`<span class="node-override-branch">分支: ${escapeHtml(String(it.branch))}</span>`);
    if (it.override_reason != null && it.override_reason !== '') parts.push(`<span class="node-override-reason">${escapeHtml(String(it.override_reason))}</span>`);
    return `<li class="node-override-item">${parts.join('')}</li>`;
  }).join('');
  return `<div class="decision-card">
    <h3>🔧 ${escapeHtml(title)}</h3>
    <ul class="node-overrides-list">${items}</ul>
  </div>`;
}

// 渲染 Stage2 diagnosis_summary（诊断摘要）
function _renderDiagnosisSummary(ds) {
  if (!ds || typeof ds !== 'object') return '';
  let html = `<div class="decision-card diagnosis-summary-card">`;
  html += `<h3>📋 诊断摘要</h3>`;
  const fields = [];
  if (ds.cycle_position != null && ds.cycle_position !== '') {
    fields.push(['周期位置', escapeHtml(bilingualCycle(ds.cycle_position)), 'cycle_position：当前所处 cycle 阶段']);
  }
  if (ds.direction != null && ds.direction !== '') {
    fields.push(['方向', escapeHtml(bilingual(ds.direction, DIRECTION_ZH)), 'direction：方向']);
  }
  if (fields.length) html += fieldGrid(fields);
  if (Array.isArray(ds.key_signals) && ds.key_signals.length) {
    html += fieldList('关键信号', ds.key_signals, 'key_signals：关键信号列表');
  }
  html += `</div>`;
  return html;
}

// ── Token 进度条 ──────────────────────────────────────────────────────
function updateTokenProgress(usage) {
  const wrap = $('#token-progress-wrap');
  if (!wrap) return;
  if (!usage) {
    wrap.classList.add('hidden');
    return;
  }
  const promptTokens = usage.prompt_tokens || 0;
  const completionTokens = usage.completion_tokens || 0;
  const totalTokens = usage.total_tokens || (promptTokens + completionTokens);
  // context_window 来自 settings.provider.context_window，默认 1_000_000
  let contextWindow = 1_000_000;
  if (currentSettings?.provider?.context_window) {
    contextWindow = currentSettings.provider.context_window;
  }
  const warnPct = currentSettings?.general?.context_warning_threshold_pct || 80;
  const dangerPct = Math.max(warnPct, 95);

  const pct = contextWindow > 0 ? Math.min(100, (totalTokens / contextWindow) * 100) : 0;
  $('#token-progress-fill').style.width = pct.toFixed(1) + '%';
  $('#token-progress-pct').textContent = pct.toFixed(1) + '%';
  $('#token-progress-detail').textContent =
    `used=${totalTokens} / window=${contextWindow} (prompt=${promptTokens}, completion=${completionTokens})`;

  wrap.classList.remove('hidden', 'warn', 'danger');
  if (pct >= dangerPct) wrap.classList.add('danger');
  else if (pct >= warnPct) wrap.classList.add('warn');

  // Phase C Task 3 SubTask 3.12：95% 上下文用量警告
  if (pct >= 95) {
    showToast('上下文用量已超过 95%，建议开始新会话', 'warning');
    // 进度条变红
    const bar = $('#token-progress-bar');
    if (bar) bar.classList.add('danger');
  }
}

function renderTokenUsage(usage) {
  if (!usage) return;
  $('#stream-usage').textContent = `Token: prompt=${usage.prompt_tokens || 0} completion=${usage.completion_tokens || 0} total=${usage.total_tokens || 0}`;
}

// ── 未来走势预期面板 ──────────────────────────────────────────────────
function renderFuturePanel(record) {
  const d = record.stage2_decision || {};
  const el = $('#future-content');
  let html = '';

  // 下一根 K 线预期
  html += renderNextBarPrediction(d.next_bar_prediction);
  // 下一周期预期
  html += renderNextCyclePrediction(d.next_cycle_prediction);

  if (!html) {
    el.innerHTML = '<div class="future-empty">本轮分析未返回未来走势预期</div>';
    return;
  }
  el.innerHTML = html;
}

function renderNextBarPrediction(pred) {
  if (!pred) return '';
  let html = '<div class="future-section"><h3>📊 下一根 K 线预期</h3>';
  if (pred.unpredictable) {
    html += '<div class="future-dir unknown">不可预测</div>';
    html += '<div class="future-reasoning">市场处于不确定状态，无法给出概率性预测</div>';
  } else {
    const probs = pred.probabilities || {};
    const bull = probs.bullish || 0;
    const bear = probs.bearish || 0;
    const neutral = probs.neutral || 0;
    // 找最大概率方向
    let dir = 'neutral', dirLabel = '中性', dirText = '中性';
    if (bull > bear && bull > neutral) { dir = 'bullish'; dirLabel = 'bullish'; dirText = '阳线偏强'; }
    else if (bear > bull && bear > neutral) { dir = 'bearish'; dirLabel = 'bearish'; dirText = '阴线偏强'; }
    html += `<div class="future-dir ${dirLabel}">${escapeHtml(dirText)}</div>`;
    html += '<div class="future-probs">';
    html += probChip('阳线', bull, dir === 'bullish');
    html += probChip('阴线', bear, dir === 'bearish');
    html += probChip('中性', neutral, dir === 'neutral');
    html += '</div>';
    // 程序补全标记：is_program_filled=true 表示模型未输出、由程序参考补全
    let reasoning = String(pred.reasoning || '');
    if (pred.is_program_filled === true) {
      reasoning = '【程序补全】模型未输出 next_bar_prediction，以下为程序参考补全：\n\n' + (reasoning || '（无）');
    }
    if (reasoning) {
      html += `<div class="future-reasoning">${escapeHtml(reasoning)}</div>`;
    }
  }
  // 使用特征（features_used）— 以 chip 列表渲染
  html += renderFeaturesUsed(pred.features_used);
  html += '</div>';
  return html;
}

function renderNextCyclePrediction(pred) {
  if (!pred) return '';
  let html = '<div class="future-section"><h3>🔄 下一个市场周期预期</h3>';
  // 顶部：周期名称（cycle）显著展示 — 卡片化
  if (pred.cycle != null && String(pred.cycle).trim() !== '') {
    html += `<div class="cycle-banner-card">
      <div class="cycle-banner-label">下一周期</div>
      <div class="cycle-banner-name">${escapeHtml(bilingualCycle(pred.cycle))}</div>
    </div>`;
  }
  if (pred.unpredictable) {
    html += '<div class="future-dir unknown">不可预测</div>';
    html += '<div class="future-reasoning">市场处于过渡或混乱状态，无法给出周期概率</div>';
  } else {
    // 方向标签
    const dir = String(pred.direction || 'neutral').toLowerCase();
    const dirText = dir === 'bullish' ? '看涨' : dir === 'bearish' ? '看跌' : '中性';
    html += `<div class="future-dir ${dir}">方向：${escapeHtml(dirText)}</div>`;
    // 8 cycle 按概率降序，Top-3 高亮
    const probs = pred.probabilities || {};
    const entries = Object.keys(CYCLE_LABELS)
      .map(k => [k, probs[k] || 0])
      .sort((a, b) => b[1] - a[1]);
    html += '<div class="future-probs">';
    entries.forEach((e, i) => {
      const [k, p] = e;
      const label = CYCLE_LABELS[k] || k;
      html += probChip(label, p, i < 3);
    });
    html += '</div>';
    // 程序补全标记：is_program_filled=true 表示模型未输出、由程序参考补全
    let reasoning = String(pred.reasoning || '');
    if (pred.is_program_filled === true) {
      reasoning = '【程序补全】模型未输出 next_cycle_prediction，以下为程序参考补全：\n\n' + (reasoning || '（无）');
    }
    if (reasoning) {
      html += `<div class="future-reasoning">${escapeHtml(reasoning)}</div>`;
    }
  }
  // 底部：使用特征（features_used）
  html += renderFeaturesUsed(pred.features_used);
  html += '</div>';
  return html;
}

// 渲染 features_used（下一根 K 线 / 下一周期共用）—— chip 列表，空数组返回空串
function renderFeaturesUsed(features) {
  if (!Array.isArray(features) || !features.length) return '';
  const chips = features.map(f => `<span class="feature-chip">${escapeHtml(String(f))}</span>`).join('');
  return `<div class="features-used-list"><span class="key">使用特征 Features Used</span><div class="chips">${chips}</div></div>`;
}

function probChip(label, value, isTop) {
  const v = Math.round(value) || 0;
  return `<span class="future-prob-chip${isTop ? ' top' : ''}">${escapeHtml(label)} ${v}%</span>`;
}

// ── 决策树 Mermaid.js 流程图（Phase K Task 21） ────────────────────────
// 把 gate_trace + decision_trace + terminal 转换为 Mermaid graph TD 语法并渲染为 SVG
// Phase D Task 4：渲染目标改为 #tree-viz-content；新增未走分支虚线节点
async function renderDecisionTreeFlowchart(payload) {
  const container = $('#tree-viz-content');
  if (!container) return;
  // Mermaid 未加载时直接降级提示
  if (typeof mermaid === 'undefined') {
    container.innerHTML = '<div class="flowchart-error">Mermaid 库未加载，无法渲染流程图</div>';
    return;
  }

  const gate = Array.isArray(payload?.gate_trace) ? payload.gate_trace : [];
  const dec = Array.isArray(payload?.decision_trace) ? payload.decision_trace : [];
  const merged = mergeTraces(gate, dec);

  if (!merged.length) {
    container.innerHTML = '<div class="flowchart-empty muted-text">无决策路径可绘制</div>';
    return;
  }

  // 节点 ID 用 n0/n1/n2...（避免 node_id 含小数点导致 Mermaid 解析失败）
  const nodes = [];
  const edges = [];

  merged.forEach((item, i) => {
    const internalId = `n${i}`;
    const origId = String(item.node_id || `step${i + 1}`);

    // 节点文本：阶段 + 节点 ID + 问题摘要 + 答案
    const question = String(item.question || '').trim();
    const questionShort = question.length > 30 ? question.slice(0, 30) + '…' : question;
    const answer = String(item.answer || '—');
    const skipped = item.skipped === true;
    const phase = String(item.phase || '').toLowerCase();
    const phaseLabel = phase === 'gate' ? '闸门' : phase === 'decision' ? '策略' : '';

    const skippedSuffix = skipped ? '（跳过）' : '';
    // 使用 ["..."] 形式的矩形节点；换行用 <br/>
    const shape = `["${phaseLabel} ${escapeHtml(origId)}<br/>${escapeHtml(questionShort)}<br/>→ ${escapeHtml(answer)}${skipped ? escapeHtml(skippedSuffix) : ''}"]`;
    nodes.push(`${internalId}${shape}`);

    // 边：连到下一节点
    if (i < merged.length - 1) {
      edges.push(`${internalId} --> n${i + 1}`);
    }

    // 未走分支：对每个 visited 节点，若 answer 是"是/通过"则未走分支是"否/不通过"，反之亦然
    // 用虚线节点 alt{i} 表示（SubTask 4.11/4.12）
    const opposite = oppositeAnswer(answer);
    if (opposite) {
      const altId = `alt${i}`;
      nodes.push(`${altId}(("未走分支：${escapeHtml(opposite)}")):::unvisited`);
      edges.push(`${internalId} -.- ${altId}`);
    }
  });

  // 终端节点（圆形 (("..."))）
  if (payload?.terminal) {
    const outcome = String(payload.terminal.outcome || 'proceed').toLowerCase();
    const outcomeZh = { trade: '交易', wait: '等待', reject: '放弃', proceed: '继续评估' }[outcome] || outcome;
    const label = payload.terminal.label ? `<br/>${escapeHtml(String(payload.terminal.label).slice(0, 40))}` : '';
    nodes.push(`terminal(("终点：${escapeHtml(outcomeZh)}${label}"))`);
    if (merged.length) {
      edges.push(`n${merged.length - 1} --> terminal`);
    }
  }

  // 构造 Mermaid 语法
  let graph = 'graph TD\n';
  // 按 answer 染色 classDef
  graph += '  classDef yes fill:#26a69a,stroke:#1e8476,color:#fff\n';
  graph += '  classDef no fill:#ef5350,stroke:#c62828,color:#fff\n';
  graph += '  classDef neutral fill:#ffc800,stroke:#b89400,color:#000\n';
  graph += '  classDef na fill:#6c757d,stroke:#495057,color:#fff\n';
  graph += '  classDef terminal fill:#2962ff,stroke:#1e3a8a,color:#fff\n';
  // 未走分支：虚线框样式（SubTask 4.12）
  graph += '  classDef unvisited fill:none,stroke:#888,stroke-dasharray: 5 5,color:#888\n';

  merged.forEach((item, i) => {
    const id = `n${i}`;
    const cls = answerColorClass(item.answer).replace('ans-', '');
    if (cls) graph += `  class ${id} ${cls}\n`;
  });
  if (payload?.terminal) graph += '  class terminal terminal\n';

  nodes.forEach(n => { graph += `  ${n}\n`; });
  edges.forEach(e => { graph += `  ${e}\n`; });

  // 渲染（mermaid.render 返回 Promise<{svg}>）
  try {
    container.innerHTML = '';
    const renderResult = await mermaid.render('tree-viz-svg', graph);
    const svg = renderResult?.svg || '';
    container.innerHTML = svg;

    // 给 SVG .node 元素加 data-node-id 属性，便于点击高亮表格行
    // Mermaid 渲染的 .node 顺序与 graph 中节点声明顺序一致：n0/alt0/n1/alt1/.../terminal
    // 仅前 merged.length 个为主路径节点（与 merged 列表一一对应），跳过 alt 节点
    const nodeEls = container.querySelectorAll('.node');
    let mainIdx = 0;
    nodeEls.forEach((nodeEl) => {
      // 通过 id 属性识别主节点（n0/n1/...）vs alt 节点（alt0/alt1/...）
      const rawId = nodeEl.id || '';
      // Mermaid 通常给节点加上形如 "flowchart-n0-XX" 的 id
      const isAlt = /alt\d+/.test(rawId);
      if (!isAlt && mainIdx < merged.length) {
        nodeEl.dataset.nodeId = String(merged[mainIdx].node_id || '');
        nodeEl.style.cursor = 'pointer';
        mainIdx++;
      }
    });
  } catch (err) {
    container.innerHTML = `<div class="flowchart-error">流程图渲染失败：${escapeHtml(String(err))}<pre>${escapeHtml(graph)}</pre></div>`;
  }
}

// 返回答案的相反值（用于未走分支标签）。无法判断时返回 null
function oppositeAnswer(answer) {
  if (answer == null) return null;
  const s = String(answer).trim();
  const lower = s.toLowerCase();
  if (/(^是$|^yes$|^true$|通过)/.test(s)) return '否';
  if (/(^否$|^no$|^false$|不通过|失败)/.test(s)) return '是';
  if (/(中性|等待|wait|neutral)/.test(lower)) return null;
  if (/(不适用|n\/a|na)/.test(lower)) return null;
  return null;
}

// 渲染未走分支（SubTask 4.11）— 简化方案：alt 虚线节点已在 renderDecisionTreeFlowchart 中绘制
// 此函数作为独立钩子保留，便于将来后端提供完整决策树节点列表时扩展
function renderUnvisitedBranches(payload) {
  // 当前实现：未走分支的虚线节点已在 renderDecisionTreeFlowchart 的 Mermaid 图中渲染
  // （对每个 visited 节点添加 alt{i} 虚线节点，标注相反答案）
  // 此处无需额外 DOM 操作；保留函数签名以匹配 spec 与未来扩展
  void payload;
}

// ── 决策树路径卡片式渲染 ────────────────────────────────────────────────
// 设计论点：决策树是 AI 思考的足迹。每个节点 = 一个"思考单元"卡片：
//   节点ID徽章 + 问题（主标题）+ 回答（彩色 chip 突出）+ 阶段/K线依据/理由（副信息）
// section 用大字标题分组；点击卡片展开高级字段（action / branch / next_node / 程序判定 / 覆盖理由 等）。
function renderDecisionTree(record) {
  const el = $('#tree-content');
  const payload = record.decision_tree;
  if (!payload) {
    el.innerHTML = '<div class="tree-empty">本轮分析未返回决策树路径</div>';
    return;
  }
  const gate = Array.isArray(payload.gate_trace) ? payload.gate_trace : [];
  const dec = Array.isArray(payload.decision_trace) ? payload.decision_trace : [];
  const merged = mergeTraces(gate, dec);
  if (!merged.length && !payload.terminal) {
    el.innerHTML = '<div class="tree-empty">决策树路径为空</div>';
    return;
  }

  let html = '';
  // 终点 banner
  if (payload.terminal) {
    const outcome = String(payload.terminal.outcome || 'proceed').toLowerCase();
    const outcomeZh = { trade: '交易', wait: '等待', reject: '放弃', proceed: '继续评估' }[outcome] || outcome;
    const label = payload.terminal.label || '';
    html += `<div class="tree-terminal-banner ${outcome}">终点：${escapeHtml(outcomeZh)}${label ? ' — ' + escapeHtml(label) : ''}</div>`;
  }
  // 闸门短路标记
  if (payload.gate_shortcircuited) {
    html += `<div class="tree-terminal-banner wait">阶段一闸门短路（gate_result=${escapeHtml(String(payload.gate_result || 'unknown'))}）</div>`;
  }

  // 卡片列表容器
  html += '<div class="trace-cards">';
  let prevSection = null;
  merged.forEach((item, i) => {
    // section 分组标题：section 字段变化时插入大字标题
    const section = String(item.section || '').trim();
    if (section && section !== prevSection) {
      html += `<div class="trace-section-title">§ ${escapeHtml(section)}</div>`;
      prevSection = section;
    }
    html += _renderTraceCard(item, i);
  });
  html += '</div>';

  // ── node_overrides 区段（Stage1 + Stage2，来自 payload 或 trace 中 overridden_by_ai=true 的条目） ──
  html += _renderDecisionTreeNodeOverrides(payload);

  el.innerHTML = html;

  // 事件委托：点击卡片头部切换展开/收起
  const cardsWrap = el.querySelector('.trace-cards');
  if (cardsWrap) {
    cardsWrap.addEventListener('click', (e) => {
      const card = e.target.closest('.trace-card');
      if (!card) return;
      // 不要在「展开高级字段」按钮内拦截
      const detail = card.querySelector('.trace-card-detail');
      if (!detail) return;
      detail.classList.toggle('hidden');
      card.classList.toggle('expanded');
    });
  }
}

// 渲染单个决策树节点卡片
function _renderTraceCard(item, i) {
  const phase = String(item.phase || '').toLowerCase();
  // phase: gate = 阶段一·闸门检查, decision = 阶段二·策略决策
  const phaseZh = phase === 'gate' ? '一·闸门' : phase === 'decision' ? '二·策略' : phase;
  const phaseTitle = phase === 'gate' ? '阶段一：闸门检查 (Stage 1 Gate)' :
                     phase === 'decision' ? '阶段二：策略决策 (Stage 2 Strategy)' : '';
  const answerInfo = formatTraceAnswer(item);
  const barBasis = normalizeBarRange(item);
  const reason = String(item.reason || '');
  const question = String(item.question || '').replace(/^§\S+\s*/, '').trim();
  const skipped = item.skipped === true;
  const nodeId = String(item.node_id || '');
  const overridden = item.overridden_by_ai === true;
  const ansCls = answerColorClass(item.answer); // ans-yes / ans-no / ans-neutral / ans-na / ''

  // 卡片头部：左侧色条 + 节点ID徽章 + 阶段标签 + 问题（主标题）+ 回答 chip
  let html = `<div class="trace-card${skipped ? ' skipped' : ''}${overridden ? ' overridden' : ''}" data-idx="${i}">`;
  html += `<div class="trace-card-head">`;
  html += `<div class="trace-card-head-left">`;
  html += `<span class="trace-card-id" title="节点 ID">${escapeHtml(nodeId)}</span>`;
  if (phaseZh) html += `<span class="trace-card-phase phase-${escapeHtml(phase)}" title="${escapeHtml(phaseTitle)}">${escapeHtml(phaseZh)}</span>`;
  if (skipped) html += `<span class="trace-card-tag tag-skipped">跳过</span>`;
  if (overridden) html += `<span class="trace-card-tag tag-overridden" title="AI 覆盖了程序判定">🔧 AI 覆盖</span>`;
  html += `</div>`;
  html += `<span class="trace-card-ans ${ansCls}" title="AI 的回答">${escapeHtml(answerInfo.text)}</span>`;
  html += `</div>`;

  // 问题主标题
  if (question) {
    html += `<div class="trace-card-question">${escapeHtml(question)}</div>`;
  }

  // 副信息行：K线依据 + 理由（同行，用分隔符）
  const metaParts = [];
  if (barBasis) metaParts.push(`<span class="trace-card-meta-item"><span class="meta-key">K线</span><span class="meta-val">${escapeHtml(barBasis)}</span></span>`);
  if (reason) metaParts.push(`<span class="trace-card-meta-item"><span class="meta-key">理由</span><span class="meta-val">${escapeHtml(reason)}</span></span>`);
  if (metaParts.length) {
    html += `<div class="trace-card-meta">${metaParts.join('')}</div>`;
  }

  // 高级字段折叠区（默认收起）
  const detailGrid = _renderTraceDetailGrid(item);
  if (detailGrid && !detailGrid.includes('trace-detail-empty')) {
    html += `<div class="trace-card-detail hidden"><div class="trace-card-detail-title">高级字段</div>${detailGrid}</div>`;
    html += `<div class="trace-card-expand-hint"><span class="trace-expand-icon" aria-hidden="true">▸</span> 展开高级字段</div>`;
  }
  html += `</div>`;
  return html;
}

// ── 决策树可视化 tab 渲染（Phase D Task 4 SubTask 4.3） ───────────────
function renderTreeViz(record) {
  const el = $('#tree-viz-content');
  if (!el) return;
  const payload = record?.decision_tree;
  if (!payload) {
    el.innerHTML = '<div class="muted-text">尚未进行交易分析</div>';
    _treeVizZoomReset(true);
    return;
  }
  // 异步渲染 Mermaid 流程图（含未走分支虚线节点）
  renderDecisionTreeFlowchart(payload).then(() => {
    // SVG 渲染完成后重置到 100%（默认显示原始大小，让文字清晰可读；
    // 用户可通过「⤢ 适配」按钮主动缩到全屏，或用 Ctrl+滚轮 / ➕➖ 缩放）。
    _treeVizZoomReset(true);
    const p = $('#tree-viz-progress');
    if (p) p.textContent = '提示：Ctrl+滚轮缩放，拖拽平移，点击「适配」查看全貌';
  });
  // 渲染未走分支（当前实现已在 Mermaid 图中绘制，此调用为钩子保留）
  renderUnvisitedBranches(payload);
  // 如果自动播放开启，启动动画
  if (currentSettings?.general?.decision_flow_auto_play) {
    const duration = Number(currentSettings?.general?.decision_flow_play_seconds || 50);
    playPathAnimation(payload, duration);
  }
}

// ── 决策树可视化 SVG 缩放/平移（Ctrl+滚轮缩放、拖拽平移、按钮缩放） ───
const _treeVizZoom = {
  scale: 1,
  tx: 0,
  ty: 0,
  MIN: 0.2,
  MAX: 3,
  inited: false,
};

function _treeVizApplyTransform() {
  const container = $('#tree-viz-content');
  if (!container) return;
  const svg = container.querySelector('svg');
  if (svg) {
    svg.style.transform = `translate(${_treeVizZoom.tx}px, ${_treeVizZoom.ty}px) scale(${_treeVizZoom.scale})`;
  }
  const pctEl = $('#tree-viz-zoom-pct');
  if (pctEl) pctEl.textContent = `${Math.round(_treeVizZoom.scale * 100)}%`;
}

function _treeVizZoomReset(silent) {
  _treeVizZoom.scale = 1;
  _treeVizZoom.tx = 0;
  _treeVizZoom.ty = 0;
  _treeVizApplyTransform();
  if (!silent) {
    const p = $('#tree-viz-progress');
    if (p) p.textContent = '已重置缩放';
  }
}

function _treeVizZoomBy(factor) {
  _treeVizZoom.scale = Math.max(_treeVizZoom.MIN, Math.min(_treeVizZoom.MAX, _treeVizZoom.scale * factor));
  _treeVizApplyTransform();
}

function _treeVizZoomFit() {
  const container = $('#tree-viz-content');
  const svg = container?.querySelector('svg');
  if (!container || !svg) return;
  // 优先用 SVG 的 viewBox（Mermaid 渲染时会设置），其次用 getBoundingClientRect
  const vb = svg.viewBox?.baseVal;
  const svgW = (vb && vb.width) || svg.width?.baseVal?.value || svg.getBoundingClientRect().width;
  const svgH = (vb && vb.height) || svg.height?.baseVal?.value || svg.getBoundingClientRect().height;
  if (!svgW || !svgH) { _treeVizZoomReset(true); return; }
  const cW = Math.max(100, container.clientWidth - 24);
  const cH = Math.max(100, container.clientHeight - 24);
  const sx = cW / svgW;
  const sy = cH / svgH;
  const fit = Math.min(sx, sy);
  _treeVizZoom.scale = Math.max(_treeVizZoom.MIN, Math.min(_treeVizZoom.MAX, fit));
  _treeVizZoom.tx = 0;
  _treeVizZoom.ty = 0;
  _treeVizApplyTransform();
  const p = $('#tree-viz-progress');
  if (p) p.textContent = `已适配窗口 (${Math.round(_treeVizZoom.scale * 100)}%)`;
}

function _initTreeVizZoomOnce() {
  if (_treeVizZoom.inited) return;
  const container = $('#tree-viz-content');
  if (!container) return;
  _treeVizZoom.inited = true;

  // Ctrl/Cmd + 滚轮缩放（避免与页面滚动冲突）
  container.addEventListener('wheel', (e) => {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.1 : (1 / 1.1);
    _treeVizZoomBy(factor);
  }, { passive: false });

  // 鼠标左键拖拽平移
  let dragging = false;
  let startX = 0, startY = 0, startTx = 0, startTy = 0;
  container.addEventListener('mousedown', (e) => {
    // 仅对容器本体或 SVG 的拖动；按钮和节点点击不拦截
    if (e.button !== 0) return;
    const target = e.target;
    // 允许在 SVG 元素和容器空白处拖动
    dragging = true;
    startX = e.clientX;
    startY = e.clientY;
    startTx = _treeVizZoom.tx;
    startTy = _treeVizZoom.ty;
    container.classList.add('grabbing');
    e.preventDefault();
  });
  document.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    _treeVizZoom.tx = startTx + (e.clientX - startX);
    _treeVizZoom.ty = startTy + (e.clientY - startY);
    _treeVizApplyTransform();
  });
  document.addEventListener('mouseup', () => {
    if (dragging) {
      dragging = false;
      container.classList.remove('grabbing');
    }
  });

  // 按钮事件
  $('#btn-tree-viz-zoom-in')?.addEventListener('click', () => _treeVizZoomBy(1.2));
  $('#btn-tree-viz-zoom-out')?.addEventListener('click', () => _treeVizZoomBy(1 / 1.2));
  $('#btn-tree-viz-zoom-fit')?.addEventListener('click', () => _treeVizZoomFit());
}

// ── 决策树路径播放动画（Phase D Task 4 SubTask 4.8/4.9） ──────────────
let treeVizPlayTimer = null;

function playPathAnimation(payload, durationSec) {
  const container = $('#tree-viz-content');
  if (!container) return;
  const gate = Array.isArray(payload?.gate_trace) ? payload.gate_trace : [];
  const dec = Array.isArray(payload?.decision_trace) ? payload.decision_trace : [];
  const merged = mergeTraces(gate, dec);
  if (!merged.length) return;

  stopPathAnimation(); // 先停止现有动画

  const progressEl = $('#tree-viz-progress');
  const playBtn = $('#btn-tree-viz-play');
  const pauseBtn = $('#btn-tree-viz-pause');
  if (playBtn) playBtn.disabled = true;
  if (pauseBtn) pauseBtn.disabled = false;

  const totalSteps = merged.length;
  const intervalMs = 40;
  const totalMs = Math.max(1, durationSec) * 1000;
  const stepsPerTick = Math.max(1, Math.ceil(totalSteps / (totalMs / intervalMs)));
  let currentStep = 0;

  // 清除所有 active-path
  container.querySelectorAll('.node').forEach(n => n.classList.remove('active-path'));

  treeVizPlayTimer = setInterval(() => {
    currentStep += stepsPerTick;
    if (currentStep >= totalSteps) {
      currentStep = totalSteps;
      stopPathAnimation();
    }
    // 高亮前 currentStep 个节点（Mermaid 渲染顺序：n0/alt0/n1/alt1/.../terminal）
    // 主路径节点（n0/n1/.../n{totalSteps-1}）按 data-node-id 过滤
    const nodes = container.querySelectorAll('.node');
    let mainHighlighted = 0;
    nodes.forEach((n) => {
      const rawId = n.id || '';
      const isAlt = /alt\d+/.test(rawId);
      if (!isAlt && n.dataset.nodeId && mainHighlighted < currentStep) {
        n.classList.add('active-path');
        mainHighlighted++;
      } else if (!isAlt && n.dataset.nodeId) {
        n.classList.remove('active-path');
      }
    });
    if (progressEl) {
      const pct = Math.round((currentStep / totalSteps) * 100);
      progressEl.textContent = `播放中… ${pct}%`;
    }
  }, intervalMs);
}

function stopPathAnimation() {
  if (treeVizPlayTimer) {
    clearInterval(treeVizPlayTimer);
    treeVizPlayTimer = null;
  }
  const playBtn = $('#btn-tree-viz-play');
  const pauseBtn = $('#btn-tree-viz-pause');
  const progressEl = $('#tree-viz-progress');
  if (playBtn) playBtn.disabled = false;
  if (pauseBtn) pauseBtn.disabled = true;
  if (progressEl && progressEl.textContent.startsWith('播放中')) {
    progressEl.textContent = '播放已停止';
  }
}

function resetPathAnimation() {
  stopPathAnimation();
  const container = $('#tree-viz-content');
  if (container) {
    container.querySelectorAll('.node').forEach(n => n.classList.remove('active-path'));
  }
  const progressEl = $('#tree-viz-progress');
  if (progressEl) progressEl.textContent = '未播放';
}

// 渲染 trace 详情区的高级字段（2 列网格）
// 字段选择原则：只展示卡片头部未展示的"高级"字段，避免与卡片头部/section 标题重复：
//   - question 已在卡片头部作为主标题 → 不重复
//   - section 已作为分组大标题 → 不重复
//   - overridden_by_ai 已在卡片头部"🔧 AI 覆盖"标签 → 不重复
// 仅展示：action / branch / next_node / program_answer / program_branch / override_reason
function _renderTraceDetailGrid(item) {
  if (!item || typeof item !== 'object') return '<div class="trace-detail-empty">（无额外字段）</div>';
  const fields = [];
  if (item.action != null && item.action !== '') fields.push(['动作 Action', escapeHtml(String(item.action))]);
  if (item.branch != null && item.branch !== '') fields.push(['分支 Branch', escapeHtml(String(item.branch))]);
  if (item.next_node != null && item.next_node !== '') fields.push(['下一节点 Next Node', escapeHtml(String(item.next_node))]);
  if (item.program_answer != null && item.program_answer !== '') fields.push(['程序判定 Program Answer', escapeHtml(String(item.program_answer))]);
  if (item.program_branch != null && item.program_branch !== '') fields.push(['程序分支 Program Branch', escapeHtml(String(item.program_branch))]);
  if (item.override_reason != null && item.override_reason !== '') fields.push(['覆盖理由 Override Reason', escapeHtml(String(item.override_reason))]);
  if (!fields.length) return '<div class="trace-detail-empty">（无额外字段）</div>';
  const items = fields.map(([k, v]) => `<div class="subfield-item"><span class="key">${k}</span><span class="val">${v}</span></div>`).join('');
  return `<div class="trace-detail-grid">${items}</div>`;
}

// 渲染决策树面板的 node_overrides 区段：
// 优先用 payload.node_overrides（若后端将来添加）；否则从 gate_trace + decision_trace 中筛选 overridden_by_ai=true 的条目
function _renderDecisionTreeNodeOverrides(payload) {
  if (!payload) return '';
  let overrides = null;
  if (Array.isArray(payload.node_overrides) && payload.node_overrides.length) {
    overrides = payload.node_overrides;
  } else {
    const gate = Array.isArray(payload.gate_trace) ? payload.gate_trace : [];
    const dec = Array.isArray(payload.decision_trace) ? payload.decision_trace : [];
    overrides = [...gate, ...dec]
      .filter(it => it && it.overridden_by_ai === true)
      .map(it => ({
        node_id: it.node_id,
        program_answer: it.program_answer,
        // trace item 的 answer 是 AI 给出的最终回答，映射到 node_override 的 ai_answer 字段
        ai_answer: it.answer,
        branch: it.branch,
        override_reason: it.override_reason,
      }));
  }
  if (!Array.isArray(overrides) || !overrides.length) return '';
  return _renderNodeOverrides(overrides, '决策树 AI 覆盖节点 (Decision Tree Node Overrides)');
}

function mergeTraces(gate, decision) {
  // 保持顺序：先 gate 后 decision（与 PyQt6 / pa_agent.ai.decision_tree.merge_traces 行为一致）
  // 必须为每条 item 注入 phase 字段，否则"阶段"列会为空
  const g = (Array.isArray(gate) ? gate : []).map(it => ({ ...(it || {}), phase: 'gate' }));
  const d = (Array.isArray(decision) ? decision : []).map(it => ({ ...(it || {}), phase: 'decision' }));
  return [...g, ...d];
}

function formatTraceAnswer(item) {
  const ans = item.answer != null ? String(item.answer) : '';
  const skipped = item.skipped === true;
  const lower = ans.toLowerCase();
  let cls = 'ans-na';
  if (/(^是$|^yes$|^true$|通过)/.test(ans)) cls = 'ans-yes';
  else if (/(^否$|^no$|^false$|不通过|失败)/.test(ans)) cls = 'ans-no';
  else if (/(中性|等待|wait|neutral)/.test(lower)) cls = 'ans-neutral';
  else if (/(不适用|n\/a|na)/.test(lower)) cls = 'ans-na';
  let text = ans || '—';
  if (skipped) text += '（跳过）';
  return { text, cls };
}

// Phase G Task 16: 按答案关键词返回染色 class，与 .trace-ans.ans-* 样式配套
// 必须识别中文「是/否/中性/等待/不适用」，否则中文回答全落到 ans-na（灰色），失去多空色彩编码
// 多空色彩编码：是=绿（看多/通过），否=红（看空/拒绝），中性/等待=黄（观望），不适用=灰
function answerColorClass(answer) {
  if (answer == null) return 'ans-na';
  const s = String(answer).toLowerCase().trim();
  // 看多 / 通过
  if (['yes', 'proceed', 'pass', 'trade', 'true', '是'].includes(s)) return 'ans-yes';
  // 看空 / 拒绝
  if (['no', 'reject', 'fail', 'false', '否'].includes(s)) return 'ans-no';
  // 中性 / 等待 / 观望
  if (['neutral', 'wait', 'unknown', 'maybe', '中性', '等待'].includes(s)) return 'ans-neutral';
  // 不适用 / 跳过
  if (['skipped', 'n_a', 'n/a', 'na', 'skip', '不适用'].includes(s)) return 'ans-na';
  return '';
}

function normalizeBarRange(item) {
  // 与后端 pa_agent.ai.decision_tree.normalize_bar_range 对齐：
  // 优先 bar_range 字符串；其次 bar_from + bar_to 组合
  if (!item) return '';
  const br = item.bar_range;
  if (br != null && String(br).trim()) return String(br);
  const bf = item.bar_from;
  const bt = item.bar_to;
  if (bf != null && bt != null) {
    const a = parseInt(bf), b = parseInt(bt);
    if (!isNaN(a) && !isNaN(b)) {
      return a === b ? `K${a}` : `K${Math.max(a, b)}-K${Math.min(a, b)}`;
    }
  }
  // 兼容旧字段名（后端不使用，但保留以防回退）
  const legacy = item.bar_basis || item.basis_bars || item.kline_basis || item.bars;
  if (legacy == null || legacy === '') return '';
  if (typeof legacy === 'string') return legacy;
  if (Array.isArray(legacy)) return legacy.join(',');
  if (typeof legacy === 'object') {
    if ('from' in legacy && 'to' in legacy) return `${legacy.from}-${legacy.to}`;
    return JSON.stringify(legacy);
  }
  return String(legacy);
}

// ── 支撑/阻力位提取（移植自 pa_agent/gui/support_resistance.py） ────
// 输入：stage1_diagnosis；输出：[{kind, low, high, label}, ...]
function extractSupportResistance(stage1) {
  if (!stage1 || typeof stage1 !== 'object') return [];
  const out = [];
  const sup = stage1.support_levels || stage1.supports || [];
  const res = stage1.resistance_levels || stage1.resistances || [];
  if (Array.isArray(sup)) {
    sup.forEach((v, i) => {
      const parsed = parseLevelValue(v);
      if (parsed) out.push({ kind: 'support', low: parsed.low, high: parsed.high, label: `支撑${i > 0 ? i + 1 : ''}` });
    });
  }
  if (Array.isArray(res)) {
    res.forEach((v, i) => {
      const parsed = parseLevelValue(v);
      if (parsed) out.push({ kind: 'resistance', low: parsed.low, high: parsed.high, label: `阻力${i > 0 ? i + 1 : ''}` });
    });
  }
  return out;
}

// 解析单条 level 值：number / "2600" / "2600-2610" / "2600~2610" / {low, high} / {price}
function parseLevelValue(v) {
  if (v == null) return null;
  if (typeof v === 'number') {
    if (!isFinite(v)) return null;
    return { low: v, high: v };
  }
  if (typeof v === 'object') {
    const low = v.low != null ? parseFloat(v.low) : null;
    const high = v.high != null ? parseFloat(v.high) : null;
    const price = v.price != null ? parseFloat(v.price) : null;
    if (low != null && high != null && !isNaN(low) && !isNaN(high)) return { low, high };
    if (price != null && !isNaN(price)) return { low: price, high: price };
    if (low != null && !isNaN(low)) return { low: low, high: low };
    if (high != null && !isNaN(high)) return { low: high, high: high };
    return null;
  }
  if (typeof v === 'string') {
    const s = v.trim();
    if (!s) return null;
    // 区间：2600-2610 / 2600~2610 / 2600—2610 / 2600到2610
    const m = s.match(/^(-?\d+(?:\.\d+)?)\s*[-~—–到至〜]\s*(-?\d+(?:\.\d+)?)$/);
    if (m) {
      const a = parseFloat(m[1]), b = parseFloat(m[2]);
      if (!isNaN(a) && !isNaN(b)) return { low: Math.min(a, b), high: Math.max(a, b) };
    }
    // 单值
    const n = parseFloat(s);
    if (!isNaN(n)) return { low: n, high: n };
    return null;
  }
  return null;
}

function formatSupportResistanceText(stage1) {
  const levels = extractSupportResistance(stage1);
  if (!levels.length) return '';
  const parts = levels.map(lv => {
    const label = lv.label || (lv.kind === 'support' ? '支撑' : '阻力');
    const range = Math.abs(lv.high - lv.low) > 1e-9 ? `${lv.low}-${lv.high}` : `${lv.low}`;
    return `${label}:${range}`;
  });
  return parts.join(' · ');
}

// 双列渲染支撑/阻力位（支撑在左，阻力在右）
function renderSupportResistanceGrid(stage1) {
  const levels = extractSupportResistance(stage1);
  if (!levels.length) return '';
  const supports = levels.filter(lv => lv.kind === 'support');
  const resistances = levels.filter(lv => lv.kind === 'resistance');
  if (!supports.length && !resistances.length) return '';

  const renderCol = (kind, list) => {
    const titleZh = kind === 'support' ? '支撑位' : '阻力位';
    const titleEn = kind === 'support' ? 'Support' : 'Resistance';
    const cls = kind === 'support' ? 'sr-support' : 'sr-resistance';
    const chips = list.map((lv, i) => {
      const label = lv.label || (kind === 'support' ? `S${i + 1}` : `R${i + 1}`);
      const range = Math.abs(lv.high - lv.low) > 1e-9 ? `${lv.low}-${lv.high}` : `${lv.low}`;
      return `<span class="sr-chip"><span class="sr-chip-label">${escapeHtml(label)}</span>${escapeHtml(range)}</span>`;
    }).join('');
    return `<div class="sr-col ${cls}">
      <div class="sr-col-title">${titleZh} ${titleEn}</div>
      <div class="sr-levels">${chips}</div>
    </div>`;
  };

  return `<div class="sr-grid">${renderCol('support', supports)}${renderCol('resistance', resistances)}</div>`;
}

// ── 盈亏比计算 ────────────────────────────────────────────────────────
function computeRiskReward(entry, tp, sl, direction) {
  const e = parseFloat(entry), t = parseFloat(tp), s = parseFloat(sl);
  if (isNaN(e) || isNaN(t) || isNaN(s)) return null;
  const dir = String(direction || '').toLowerCase();
  const isShort = dir === 'short' || dir === '做空' || dir === 'sell';
  let risk, reward;
  if (isShort) {
    risk = s - e;   // short: SL 在上，risk = sl - entry
    reward = e - t; // short: TP 在下，reward = entry - tp
  } else {
    risk = e - s;   // long: SL 在下，risk = entry - sl
    reward = t - e; // long: TP 在上，reward = tp - entry
  }
  if (risk <= 0 || reward <= 0) return null;
  const ratio = reward / risk;
  const ratioText = `${ratio.toFixed(2)}:1 (risk=${risk.toFixed(2)}, reward=${reward.toFixed(2)})`;
  return { ratio, risk, reward, ratioText };
}

function parseWinRate(v) {
  if (v == null) return null;
  if (typeof v === 'number') return Math.max(0, Math.min(100, v));
  const s = String(v).replace('%', '').trim();
  const n = parseFloat(s);
  return isNaN(n) ? null : Math.max(0, Math.min(100, n));
}

function parsePercent(v) {
  const n = parseWinRate(v);
  return n == null ? 0 : Math.round(n);
}

// ── Prompt 展示已迁移到 stage-block 内部（见 setStagePrompt / resetStageBlock） ─

// ── Chat（追问嵌入实时 tab，Phase C Task 3） ─────────────────────────
function enableChat() {
  const input = $('#chat-input');
  const sendBtn = $('#btn-chat-send');
  if (input) input.disabled = false;
  if (sendBtn) sendBtn.disabled = false;
}

async function sendChat() {
  const input = $('#chat-input');
  const sendBtn = $('#btn-chat-send');
  if (!input || !sendBtn) return;
  const text = input.value.trim();
  if (!text) return;

  // 如果正在发送，点击按钮 = 中断
  if (chatAbortController) {
    chatAbortController.abort();
    return;
  }

  lastUserMessage = text;
  input.value = '';
  appendChatMsg('user', text);
  appendChatMsg('assistant', '');

  sendBtn.textContent = '停止';
  sendBtn.classList.add('btn-danger');

  chatAbortController = new AbortController();
  chatReasoningText = '';
  chatContentText = '';

  const recordId = lastRecord ? `${lastRecord.symbol}_${lastRecord.timeframe}_${Date.now()}` : `chat_${Date.now()}`;
  const url = `/api/chat/stream?text=${encodeURIComponent(text)}&record_id=${encodeURIComponent(recordId)}&attach_kline_snapshot=true`;

  try {
    const resp = await fetch(url, { signal: chatAbortController.signal });
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const evt = JSON.parse(line.slice(6));
          if (evt.type === 'reasoning_token') {
            chatReasoningText += evt.chunk || '';
            stageCharCounts.chat.reasoning = chatReasoningText.length;
            updateStreamStats();
            const rEl = $('#chat-reasoning');
            if (rEl) rEl.textContent = chatReasoningText;
          } else if (evt.type === 'content_token') {
            chatContentText += evt.chunk || '';
            stageCharCounts.chat.content = chatContentText.length;
            updateStreamStats();
            const cEl = $('#chat-content');
            if (cEl) cEl.textContent = chatContentText;
          } else if (evt.type === 'done') {
            break;
          } else if (evt.type === 'error') {
            const cEl = $('#chat-content');
            if (cEl) cEl.textContent = `[错误] ${evt.message || '未知错误'}`;
            break;
          }
        } catch (e) { /* ignore parse errors */ }
      }
    }
  } catch (err) {
    if (err.name === 'AbortError') {
      const cEl = $('#chat-content');
      if (cEl) cEl.textContent += '\n[已中断]';
    } else {
      const cEl = $('#chat-content');
      if (cEl) cEl.textContent = `[错误] ${err.message}`;
    }
  } finally {
    chatAbortController = null;
    sendBtn.textContent = '发送';
    sendBtn.classList.remove('btn-danger');
  }
}

function appendChatMsg(role, text) {
  // 追加到实时 tab 的流式区（#tab-stream）
  const streamPanel = $('#tab-stream');
  if (!streamPanel) return null;

  const div = document.createElement('div');
  div.className = `chat-msg ${role}`;
  if (role === 'user') {
    // 用户消息红色插入
    div.innerHTML = `<div class="bubble" style="color: #ef5350;">【追问】${escapeHtml(text)}</div>`;
  } else {
    // AI 消息：reasoning + content 两个子元素
    div.innerHTML = `<div class="reasoning muted-text" id="chat-reasoning"></div><div class="bubble" id="chat-content"></div>`;
  }
  streamPanel.appendChild(div);
  streamPanel.scrollTop = streamPanel.scrollHeight;
  return div;
}

// 清空实时 tab 中的追问消息（保留 stage1/stage2 流式输出）
function clearChatOutput() {
  const streamPanel = $('#tab-stream');
  if (!streamPanel) return;
  streamPanel.querySelectorAll('.chat-msg').forEach(el => el.remove());
  chatReasoningText = '';
  chatContentText = '';
  stageCharCounts.chat = { reasoning: 0, content: 0 };
  updateStreamStats();
}

// 重发上一条用户追问（丢弃 reasoning 节省 token）
async function resendLastChat() {
  if (!lastUserMessage) return;
  clearChatOutput();
  const input = $('#chat-input');
  if (input) {
    input.value = lastUserMessage;
    await sendChat();
  }
}

// 字数统计：阶段一/二/追问 的 reasoning + content 字数
function updateStreamStats() {
  const el = $('#stream-stats');
  if (!el) return;
  const s1 = stageCharCounts.stage1;
  const s2 = stageCharCounts.stage2;
  const c = stageCharCounts.chat;
  const parts = [];
  if (s1.reasoning || s1.content) parts.push(`阶段一：思考${s1.reasoning}+回答${s1.content}字`);
  if (s2.reasoning || s2.content) parts.push(`阶段二：思考${s2.reasoning}+回答${s2.content}字`);
  if (c.reasoning || c.content) parts.push(`追问：思考${c.reasoning}+回答${c.content}字`);
  if (parts.length) {
    el.textContent = parts.join(' / ');
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

// ── 历史分析记录（回看 / replay） ─────────────────────────────────────
// 加载当前 (exchange, symbol, timeframe) 的最近 50 条历史分析记录并渲染到 popover 列表
async function loadHistoryList() {
  try {
    const exchange = $('#ds-exchange').value || currentSettings?.general?.last_tradingview_exchange || '';
    const symbol = $('#ds-symbol').value || currentSettings?.general?.last_symbol || 'BTCUSDT';
    const timeframe = $('#ds-timeframe').value || currentSettings?.general?.last_timeframe || '1d';
    if (!exchange || !symbol || !timeframe) return;
    const data = await API.get(`/api/records?exchange=${encodeURIComponent(exchange)}&symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&limit=50`);
    renderHistoryList(data || []);
  } catch (e) {
    console.error('loadHistoryList:', e);
    renderHistoryList([]);
  }
}

// 渲染历史记录列表项到 popover
function renderHistoryList(records) {
  const list = $('#history-list');
  if (!list) return;
  if (!records.length) {
    list.innerHTML = '<div class="history-empty muted-text">暂无历史记录</div>';
    return;
  }
  list.innerHTML = records.map(r => {
    const time = r.timestamp ? new Date(r.timestamp).toLocaleString('zh-CN', { hour12: false }) : '';
    const decision = formatDecisionSummary(r);
    const recordId = encodeURIComponent(r.record_id || '');
    // close bar 时间格式化：仅 last_close_bar_iso 非空时渲染 span
    const closeBarTime = r.last_close_bar_iso
      ? new Date(r.last_close_bar_iso).toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
      : '';
    return `<div class="history-item" data-record-id="${recordId}">
      <span class="history-item-time">${escapeHtml(time)}</span>
      ${closeBarTime ? `<span class="history-item-close-bar">📍 ${escapeHtml(closeBarTime)}</span>` : ''}
      <span class="history-item-decision">${escapeHtml(decision)}</span>
      <button class="history-item-delete" title="删除" data-record-id="${recordId}">✕</button>
    </div>`;
  }).join('');
  // 绑定点击：外层 .history-item → replayRecord；内层 .history-item-delete → deleteRecord（阻止冒泡）
  $$('#history-list .history-item').forEach(item => {
    item.addEventListener('click', () => replayRecord(decodeURIComponent(item.dataset.recordId)));
  });
  $$('#history-list .history-item-delete').forEach(btn => {
    btn.addEventListener('click', (event) => {
      event.stopPropagation();
      deleteRecord(decodeURIComponent(btn.dataset.recordId));
    });
  });
}

// 把单条历史摘要格式化为 "下单类型 · 方向" 或 "不下单"
function formatDecisionSummary(r) {
  if (r.terminal_outcome === 'no_trade' || r.order_type === 'no_order') return '不下单';
  const order = r.order_type ? (ORDER_TYPE_ZH[r.order_type.toLowerCase()] || r.order_type) : '';
  const dir = r.direction ? (DIRECTION_ZH[r.direction.toLowerCase()] || r.direction) : '';
  return `${order} · ${dir}`.replace(/^ · | · $/g, '').trim() || '—';
}

// 拉取完整 AnalysisRecord 并重新渲染三个 tab + 显示回看 badge
async function replayRecord(recordId) {
  if (!recordId) return;
  try {
    const data = await API.get(`/api/records/${encodeURIComponent(recordId)}`);
    lastRecord = data;
    isReplaying = true;
    // 重新渲染所有 tab
    if (typeof renderDecision === 'function') renderDecision(lastRecord);
    if (typeof renderDecisionTree === 'function') renderDecisionTree(lastRecord);
    if (typeof renderFuturePanel === 'function') renderFuturePanel(lastRecord);
    if (typeof renderRaw === 'function') renderRaw(lastRecord);
    if (typeof renderDebug === 'function') renderDebug(lastRecord);
    // Phase A Task 1.1：补充决策树可视化回显
    if (typeof renderTreeViz === 'function') renderTreeViz(lastRecord);
    // Phase A Task 1.2：补充实时 tab 历史回显
    if (typeof renderStreamFromRecord === 'function') renderStreamFromRecord(lastRecord);
    // 显示回看 badge
    showReplayBadge(data);
    // 关闭 popover
    $('#history-popover').classList.add('hidden');
    // 显示"返回实时"按钮
    $('#btn-back-to-live').classList.remove('hidden');
    // 切到决策 tab
    $$('.sidebar-tabs .tab').forEach(b => b.classList.remove('active'));
    document.querySelector('.sidebar-tabs .tab[data-tab="decision"]')?.classList.add('active');
    $$('.tab-panel').forEach(p => p.classList.remove('active'));
    $('#tab-decision').classList.add('active');
  } catch (e) {
    console.error('replayRecord:', e);
    alert('加载历史记录失败');
  }
}

// 显示回看 badge，并把记录时间填入 #replay-time
function showReplayBadge(record) {
  const badge = $('#replay-badge');
  if (!badge) return;
  const time = record?.meta?.timestamp_local_iso || record?.meta?.timestamp || '';
  $('#replay-time').textContent = time ? new Date(time).toLocaleString('zh-CN', { hour12: false }) : '';
  badge.classList.remove('hidden');
}

// 隐藏回看 badge 和"返回实时"按钮，重置 isReplaying
function hideReplayBadge() {
  const badge = $('#replay-badge');
  if (badge) badge.classList.add('hidden');
  const btnBack = $('#btn-back-to-live');
  if (btnBack) btnBack.classList.add('hidden');
  isReplaying = false;
}

// 删除历史记录：二次确认 → DELETE /api/records/{record_id} → 刷新列表
async function deleteRecord(recordId) {
  if (!recordId) return;
  if (!confirm('确定删除此条历史记录？')) return;
  try {
    await API.delete(`/api/records/${encodeURIComponent(recordId)}`);
    showToast('已删除');
    loadHistoryList();  // 刷新列表
  } catch (e) {
    console.error('deleteRecord:', e);
    const msg = e?.status === 404 ? '删除失败：记录不存在' : `删除失败：${e.message || e}`;
    showToast(msg);
  }
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

// ── 「原始」tab：展示 AI 请求/响应原始数据 ────────────────────────────
// 数据来源优先级：record.raw_debug_payload（SSE done 事件附带）→
//                record.stage1_messages / record.stage1_response 等（历史回看 fallback）
let rawCurrentTurn = 'stage1'; // 'stage1' | 'stage2' | 'exception'

function renderRaw(record) {
  const el = $('#raw-content');
  if (!el || !record) return;

  const payload = record.raw_debug_payload || {};
  const hasException = !!record.exception;

  // 更新异常轮次按钮可见性
  const excBtn = $('.raw-turn-exception');
  if (excBtn) excBtn.hidden = !hasException;

  // 有异常时自动聚焦异常轮次（仅当用户未手动选中异常时才自动切）
  if (hasException && rawCurrentTurn !== 'exception') {
    focusExceptionTurn();
  }

  // 更新轮次按钮标签（含缓存命中率）
  updateRawTurnButtons(payload);

  // 渲染当前轮次内容
  renderRawTurnContent(rawCurrentTurn, record, payload);
}

// 从 OpenAI 风格 messages list 中安全取出第 index 条的 content 字符串
function _extractMessageContent(messages, index) {
  if (!Array.isArray(messages) || index < 0 || index >= messages.length) return '';
  const item = messages[index];
  if (!item || typeof item !== 'object') return '';
  const content = item.content;
  if (content == null) return '';
  return typeof content === 'string' ? content : JSON.stringify(content);
}

function updateRawTurnButtons(payload) {
  const s1Btn = $('.raw-turn-btn[data-turn="stage1"]');
  const s2Btn = $('.raw-turn-btn[data-turn="stage2"]');
  if (s1Btn && payload.stage1_cache_hit_pct != null) {
    s1Btn.textContent = `阶段一诊断 [${payload.stage1_cache_hit_pct}% 缓存]`;
  } else if (s1Btn) {
    s1Btn.textContent = '阶段一诊断';
  }
  if (s2Btn && payload.stage2_cache_hit_pct != null) {
    s2Btn.textContent = `阶段二决策 [${payload.stage2_cache_hit_pct}% 缓存]`;
  } else if (s2Btn) {
    s2Btn.textContent = '阶段二决策';
  }
}

function focusExceptionTurn() {
  rawCurrentTurn = 'exception';
  document.querySelectorAll('.raw-turn-btn').forEach(b => b.classList.remove('active'));
  const excBtn = $('.raw-turn-exception');
  if (excBtn) {
    excBtn.classList.add('active');
    excBtn.hidden = false;
  }
}

function renderRawTurnContent(turn, record, payload) {
  const el = $('#raw-content');
  if (!el) return;

  // fallback：raw_debug_payload 缺失时直接从 record 顶层取
  const rdp = (payload && typeof payload === 'object') ? payload : {};
  const hasRdp = !!(record.raw_debug_payload && typeof record.raw_debug_payload === 'object');

  let systemPrompt = '', userPrompt = '', rawResponse = '', validationInfo = '';
  let kvCacheBanner = '';

  if (turn === 'stage1') {
    if (hasRdp) {
      systemPrompt = rdp.stage1_system_prompt || '';
      userPrompt = rdp.stage1_user_prompt || '';
      rawResponse = rdp.stage1_raw_response;
    } else {
      const s1Messages = Array.isArray(record.stage1_messages) ? record.stage1_messages : [];
      systemPrompt = _extractMessageContent(s1Messages, 0);
      userPrompt = _extractMessageContent(s1Messages, 1);
      rawResponse = record.stage1_response;
    }
    const v = rdp.validation || {};
    const valid = hasRdp ? v.stage1_valid : (record.stage1_diagnosis != null);
    validationInfo = `JSON 解析：${valid ? '✓ 通过' : '✗ 失败'}\n`;
    if (hasRdp) {
      if (v.stage1_missing_fields && v.stage1_missing_fields.length) {
        validationInfo += `缺失字段：${v.stage1_missing_fields.join(', ')}\n`;
      }
      if (v.stage1_invalid_fields && v.stage1_invalid_fields.length) {
        validationInfo += `无效字段：${v.stage1_invalid_fields.join(', ')}\n`;
      }
    } else if (record.exception && typeof record.exception === 'object') {
      const mf = record.exception.missing_fields;
      const ifo = record.exception.invalid_fields;
      if (Array.isArray(mf) && mf.length) validationInfo += `缺失字段：${mf.join(', ')}\n`;
      if (Array.isArray(ifo) && ifo.length) validationInfo += `无效字段：${ifo.join(', ')}\n`;
    }
    kvCacheBanner = payload.stage1_cache_hit_pct != null
      ? `KV Cache: 命中 ${payload.stage1_cache_hit_pct}%`
      : '';
  } else if (turn === 'stage2') {
    if (hasRdp) {
      systemPrompt = rdp.stage2_system_prompt || '';
      userPrompt = rdp.stage2_user_prompt || '';
      rawResponse = rdp.stage2_raw_response;
    } else {
      const s2Messages = Array.isArray(record.stage2_messages) ? record.stage2_messages : [];
      systemPrompt = _extractMessageContent(s2Messages, 0);
      userPrompt = _extractMessageContent(s2Messages, 1);
      rawResponse = record.stage2_response;
    }
    const v = rdp.validation || {};
    const valid = hasRdp ? v.stage2_valid : (record.stage2_decision != null);
    validationInfo = `JSON 解析：${valid ? '✓ 通过' : '✗ 失败'}\n`;
    kvCacheBanner = payload.stage2_cache_hit_pct != null
      ? `KV Cache: 命中 ${payload.stage2_cache_hit_pct}%`
      : '';
  } else if (turn === 'exception') {
    const exception = hasRdp ? rdp.exception : record.exception;
    validationInfo = exception
      ? (typeof exception === 'string' ? exception : JSON.stringify(exception, null, 2))
      : '无异常';
  }

  // raw_response 可能是 dict / null / str
  const rawStr = rawResponse == null
    ? '(无)'
    : (typeof rawResponse === 'string' ? rawResponse : JSON.stringify(rawResponse, null, 2));

  let html = '<div class="raw-tab-content">';
  if (kvCacheBanner) {
    html += `<div class="kv-cache-banner">${escapeHtml(kvCacheBanner)}</div>`;
  }
  html += `<details class="raw-tab-section" open><summary>📝 System Prompt</summary><pre class="raw-json-pre">${escapeHtml(systemPrompt || '（无）')}</pre></details>`;
  html += `<details class="raw-tab-section" open><summary>📝 User Prompt</summary><pre class="raw-json-pre">${escapeHtml(userPrompt || '（无）')}</pre></details>`;
  html += `<details class="raw-tab-section" open><summary>💡 AI 原始响应</summary><pre class="raw-json-pre">${escapeHtml(rawStr)}</pre></details>`;
  html += `<details class="raw-tab-section"><summary>⚠️ 验证 / 异常信息</summary><pre class="raw-json-pre">${escapeHtml(validationInfo || '（无）')}</pre></details>`;
  html += '</div>';

  // 保留原有的复制/导出按钮
  html += `<div class="raw-tab-buttons">
    <button type="button" class="compact-btn" data-action="copy-debug">📋 复制调试信息</button>
    <button type="button" class="compact-btn" data-action="export-json">💾 导出 JSON</button>
  </div>`;

  el.innerHTML = html;
}

// 复制整条 record JSON 到剪贴板，用于 bug report
function copyDebugInfo() {
  if (!lastRecord) {
    showToast('暂无分析记录可复制');
    return;
  }
  try {
    const text = JSON.stringify(lastRecord, null, 2);
    navigator.clipboard.writeText(text).then(
      () => showToast('已复制到剪贴板'),
      (err) => {
        console.error('clipboard write failed:', err);
        showToast('复制失败：' + (err && err.message ? err.message : '浏览器拒绝'));
      }
    );
  } catch (e) {
    console.error('copyDebugInfo:', e);
    showToast('复制失败：' + e.message);
  }
}

// 导出当前 lastRecord 为 .json 文件下载
function exportRecordJson() {
  if (!lastRecord) {
    showToast('暂无分析记录可导出');
    return;
  }
  try {
    const text = JSON.stringify(lastRecord, null, 2);
    const blob = new Blob([text], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const symbol = (lastRecord.symbol || 'unknown').replace(/[^A-Za-z0-9_-]/g, '');
    const timeframe = (lastRecord.timeframe || 'unknown').replace(/[^A-Za-z0-9_-]/g, '');
    // 时间戳取 meta.timestamp_local_iso（如有），否则用 Date.now()
    let ts = '';
    if (lastRecord.meta && lastRecord.meta.timestamp_local_iso) {
      ts = String(lastRecord.meta.timestamp_local_iso).replace(/[^0-9T_-]/g, '').replace('T', '_');
    } else {
      ts = new Date().toISOString().replace(/[^0-9T_-]/g, '').replace('T', '_');
    }
    const a = document.createElement('a');
    a.href = url;
    a.download = `${ts}_${symbol}_${timeframe}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    // 释放 ObjectURL 避免内存泄漏
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    showToast('已开始下载 JSON 文件');
  } catch (e) {
    console.error('exportRecordJson:', e);
    showToast('导出失败：' + e.message);
  }
}

// 临时 Toast 提示（不依赖 toast-container，使用简易浮层）
function showToast(message) {
  // 复用已有 toast-container（Phase E 引入）；不存在则创建临时浮层
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    container.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:200;display:flex;flex-direction:column;gap:8px;';
    document.body.appendChild(container);
  }
  const toast = document.createElement('div');
  toast.className = 'toast-card';
  toast.style.cssText = 'background:#1e222d;color:#d1d4dc;border:1px solid #363c4e;border-radius:6px;padding:8px 14px;font-size:13px;box-shadow:0 4px 12px rgba(0,0,0,0.4);max-width:280px;';
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => {
    if (toast.parentNode) toast.parentNode.removeChild(toast);
  }, 2000);
}

// ── 下单机会提醒（Phase E Task 12） ────────────────────────────────────
// Toast 卡片 + 浏览器通知 + 蜂鸣音三种方式同步提醒用户出现下单机会。
// 触发条件：order_type ∈ [limit, market, stop] 且 trade_confidence >= 阈值。
// 受 settings.general.alert_on_order_opportunity 开关控制（默认 true）。

// 格式化价格：None/空 → "—"；数字 → 去尾零；其他 → 原样字符串
function _fmtToastPrice(value) {
  if (value == null || value === '') return '—';
  const n = Number(value);
  if (!isNaN(n) && isFinite(n)) return String(n);
  return String(value);
}

// 显示下单机会 Toast 卡片（120 秒自动关闭）
function showOrderToast(decision) {
  if (!decision || typeof decision !== 'object') return;
  const container = document.getElementById('toast-container');
  if (!container) return;

  const direction = bilingual(decision.order_direction, DIRECTION_ZH) || '—';
  const orderType = bilingual(decision.order_type, ORDER_TYPE_ZH) || '—';
  const entry = _fmtToastPrice(decision.entry_price);
  const sl = _fmtToastPrice(decision.stop_loss_price);
  const tp1 = _fmtToastPrice(decision.take_profit_price);

  const toast = document.createElement('div');
  toast.className = 'toast-card';
  toast.innerHTML = `
    <div class="toast-header">
      <span class="toast-icon">📈</span>
      <span class="toast-title">下单机会</span>
      <button class="toast-close" aria-label="关闭" type="button">×</button>
    </div>
    <div class="toast-body">
      <div class="toast-row"><span>方向</span><span>${escapeHtml(direction)}</span></div>
      <div class="toast-row"><span>方式</span><span>${escapeHtml(orderType)}</span></div>
      <div class="toast-row"><span>入场</span><span>${escapeHtml(entry)}</span></div>
      <div class="toast-row"><span>止损</span><span>${escapeHtml(sl)}</span></div>
      <div class="toast-row"><span>TP1</span><span>${escapeHtml(tp1)}</span></div>
    </div>
    <div class="toast-actions">
      <button class="toast-btn-view" type="button">查看决策</button>
    </div>
  `;

  // 关闭按钮：点击移除 Toast
  toast.querySelector('.toast-close').addEventListener('click', () => {
    if (toast.parentNode) toast.parentNode.removeChild(toast);
  });
  // 「查看决策」按钮：关闭 Toast + 切换到 tab-decision
  toast.querySelector('.toast-btn-view').addEventListener('click', () => {
    if (toast.parentNode) toast.parentNode.removeChild(toast);
    $$('.sidebar-tabs .tab').forEach(b => b.classList.remove('active'));
    const decisionTab = document.querySelector('.sidebar-tabs .tab[data-tab="decision"]');
    if (decisionTab) decisionTab.classList.add('active');
    $$('.tab-panel').forEach(p => p.classList.remove('active'));
    $('#tab-decision').classList.add('active');
  });

  container.appendChild(toast);
  // 120 秒后自动关闭
  setTimeout(() => {
    if (toast.parentNode) toast.parentNode.removeChild(toast);
  }, 120000);
}

// 浏览器通知：请求权限并弹出系统通知
function notifyOrderOpportunity(decision) {
  if (!decision || typeof decision !== 'object') return;
  if (!('Notification' in window)) return;
  if (Notification.permission === 'default') {
    try { Notification.requestPermission(); } catch (e) { console.warn('requestPermission failed', e); }
  }
  if (Notification.permission !== 'granted') return;
  const direction = bilingual(decision.order_direction, DIRECTION_ZH) || '—';
  const orderType = bilingual(decision.order_type, ORDER_TYPE_ZH) || '—';
  const entry = _fmtToastPrice(decision.entry_price);
  const sl = _fmtToastPrice(decision.stop_loss_price);
  const tp1 = _fmtToastPrice(decision.take_profit_price);
  const body = `${direction} · ${orderType} · 入场 ${entry} · 止损 ${sl} · TP1 ${tp1}`;
  try {
    new Notification('📈 下单机会', { body, icon: '/static/favicon.ico' });
  } catch (e) {
    console.warn('Notification failed:', e);
  }
}

// 蜂鸣提醒音：Web Audio API 880Hz 200ms 正弦波
function playBeep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.type = 'sine';
    osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.3, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.2);
    osc.start();
    osc.stop(ctx.currentTime + 0.2);
    setTimeout(() => { try { ctx.close(); } catch (_) {} }, 300);
  } catch (e) {
    console.warn('beep failed', e);
  }
}

// 下单机会提醒统一入口：检查开关 + 阈值后依次触发 Toast / 通知 / 蜂鸣
// 触发条件：
//   1. settings.general.alert_on_order_opportunity !== false（默认 true）
//   2. decision.order_type ∈ [limit, market, stop]
//   3. decision.trade_confidence >= settings.general.decision_confidence_threshold（默认 40）
function triggerOrderAlertIfNeeded(record) {
  if (!record || typeof record !== 'object') return;
  // 开关检查：默认 true，仅在显式 false 时跳过（避免 undefined 误判）
  if (currentSettings?.general?.alert_on_order_opportunity === false) return;
  const decision = record.stage2_decision || {};
  const orderType = String(decision.order_type || '').toLowerCase();
  const confidence = Number(decision.trade_confidence || 0);
  const threshold = Number(currentSettings?.general?.decision_confidence_threshold || 40);
  if (!['limit', 'market', 'stop'].includes(orderType)) return;
  if (confidence < threshold) return;
  showOrderToast(decision);
  notifyOrderOpportunity(decision);
  playBeep();
}

// ── 「调试」tab：展示本次分析加载的策略文件与经验库 ───────────────────
// 数据来源优先级：record.debug_files_payload（SSE done 事件附带）→
//                record.strategy_files_used / record.experience_loaded（历史回看 fallback）
function renderDebug(record) {
  const el = $('#debug-content');
  if (!el || !record) return;

  const dfp = record.debug_files_payload;
  let stage1Files, stage2Files, experienceFiles, expCount;

  if (dfp && typeof dfp === 'object') {
    stage1Files = Array.isArray(dfp.stage1_files) ? dfp.stage1_files : [];
    stage2Files = Array.isArray(dfp.stage2_files) ? dfp.stage2_files : [];
    experienceFiles = Array.isArray(dfp.experience_loaded) ? dfp.experience_loaded : [];
    expCount = dfp.experience_count || { success: 0, failure: 0 };
  } else {
    // fallback：从 record 顶层字段直接提取
    const strategy = Array.isArray(record.strategy_files_used) ? record.strategy_files_used : [];
    stage1Files = [];  // 历史记录无法区分 stage1/stage2，全部归入 stage2
    stage2Files = strategy;
    const exp = Array.isArray(record.experience_loaded) ? record.experience_loaded : [];
    experienceFiles = exp.map(e => (typeof e === 'object' && e ? (e.filename || '') : String(e || ''))).filter(Boolean);
    let success = 0, failure = 0;
    exp.forEach(e => {
      const t = String((typeof e === 'object' && e ? e.case_type : '') || '').toLowerCase();
      if (t === 'success') success++;
      else if (t === 'failure') failure++;
    });
    expCount = { success, failure };
  }

  const totalExp = (expCount.success || 0) + (expCount.failure || 0);

  let html = '';
  // 阶段一策略文件
  html += `<div class="debug-tab-card">`;
  html += `<div class="debug-tab-card-title">📄 阶段一策略文件 (Stage 1 Strategy Files)</div>`;
  html += `<div class="debug-tab-card-count">${stage1Files.length} 个文件</div>`;
  if (stage1Files.length) {
    const chips = stage1Files.map(f => `<span class="debug-chip">${escapeHtml(String(f))}</span>`).join('');
    html += `<div class="debug-chips">${chips}</div>`;
  } else {
    html += `<div class="muted-text">无（阶段一通常使用静态 system prompt，无动态文件）</div>`;
  }
  html += `<div class="debug-extra-note">阶段一另含内置 JSON 输出格式说明（非 txt）</div>`;
  html += `</div>`;

  // 阶段二策略文件
  html += `<div class="debug-tab-card">`;
  html += `<div class="debug-tab-card-title">📄 阶段二策略文件 (Stage 2 Strategy Files)</div>`;
  html += `<div class="debug-tab-card-count">${stage2Files.length} 个文件</div>`;
  if (stage2Files.length) {
    const chips = stage2Files.map(f => `<span class="debug-chip">${escapeHtml(String(f))}</span>`).join('');
    html += `<div class="debug-chips">${chips}</div>`;
  } else {
    html += `<div class="muted-text">无（本次未动态加载策略文件）</div>`;
  }
  html += `<div class="debug-extra-note">阶段二另含内置 JSON 决策契约（非 txt）</div>`;
  html += `</div>`;

  // 经验库
  html += `<div class="debug-tab-card">`;
  html += `<div class="debug-tab-card-title">📚 经验库 (Experience Library)</div>`;
  html += `<div class="debug-tab-card-count">共 ${totalExp} 条案例（成功 ${expCount.success || 0} · 失败 ${expCount.failure || 0}）</div>`;
  if (experienceFiles.length) {
    const chips = experienceFiles.map(f => `<span class="debug-chip">${escapeHtml(String(f))}</span>`).join('');
    html += `<div class="debug-chips">${chips}</div>`;
  } else {
    html += `<div class="muted-text">本次未加载经验库案例</div>`;
  }
  if (totalExp > 0) {
    html += `<div class="debug-extra-note">阶段二另注入经验库 ${totalExp} 条（非 txt）</div>`;
  }
  html += `</div>`;

  el.innerHTML = html;
}

// ── Phase A Task 2：历史回看时把 raw_debug_payload 渲染到 stream tab ────
// 把 record.raw_debug_payload 中的 stage1/stage2 system/user prompt +
// reasoning_content + content 回显到 #stage1-* / #stage2-* DOM，
// 并在 stream tab 顶部显示回看 banner 提示「以下为历史记录回显，非实时流」。
function renderStreamFromRecord(record) {
  if (!record) return;

  const streamTab = $('#tab-stream');
  if (!streamTab) return;

  // 1) 移除已存在的 .replay-banner（避免重复插入）
  streamTab.querySelectorAll('.replay-banner').forEach(el => el.remove());

  // 2) 构造回看 banner
  const timestamp = record?.meta?.timestamp_local_iso || record?.meta?.timestamp || '';
  const timeStr = timestamp
    ? new Date(timestamp).toLocaleString('zh-CN', { hour12: false })
    : '';
  const banner = document.createElement('div');
  banner.className = 'replay-banner';

  const payload = record.raw_debug_payload;
  const hasPayload = !!(payload && typeof payload === 'object');

  if (!hasPayload) {
    // SubTask 2.4：raw_debug_payload 不存在的旧记录
    banner.textContent = '⚠️ 此记录无原始 prompt/response 数据，仅显示决策结果';
  } else {
    banner.textContent = timeStr
      ? `⏪ 以下为历史记录回显（${timeStr}），非实时流`
      : '⏪ 以下为历史记录回显，非实时流';
  }

  // 插入到 #stream-stats 上方（若 #stream-stats 不存在则插到 stream tab 顶部）
  const streamStats = $('#stream-stats');
  if (streamStats) {
    streamStats.parentNode.insertBefore(banner, streamStats);
  } else {
    streamTab.insertBefore(banner, streamTab.firstChild);
  }

  // 3) 清空 stage1/stage2 reasoning + content DOM
  const stage1Reasoning = $('#stage1-reasoning');
  const stage1Content = $('#stage1-content');
  const stage2Reasoning = $('#stage2-reasoning');
  const stage2Content = $('#stage2-content');
  if (stage1Reasoning) stage1Reasoning.textContent = '';
  if (stage1Content) stage1Content.textContent = '';
  if (stage2Reasoning) stage2Reasoning.textContent = '';
  if (stage2Content) stage2Content.textContent = '';

  // 4) raw_debug_payload 不存在：仅显示 banner，不渲染 prompt/response
  if (!hasPayload) return;

  // 5) 渲染 stage1 / stage2 system + user prompt
  setStagePrompt(1, payload.stage1_system_prompt || '', payload.stage1_user_prompt || '');
  setStagePrompt(2, payload.stage2_system_prompt || '', payload.stage2_user_prompt || '');

  // 6) 渲染 stage1 / stage2 reasoning_content + content
  // raw_response 结构：{ content: ..., reasoning_content: ... }（可能为 null/string/dict）
  const s1Resp = payload.stage1_raw_response;
  const s2Resp = payload.stage2_raw_response;
  const s1Reasoning = _extractReasoningContent(s1Resp);
  const s1Content = _extractContent(s1Resp);
  const s2Reasoning = _extractReasoningContent(s2Resp);
  const s2Content = _extractContent(s2Resp);

  if (stage1Reasoning && s1Reasoning) stage1Reasoning.textContent = s1Reasoning;
  if (stage1Content && s1Content) stage1Content.textContent = s1Content;
  if (stage2Reasoning && s2Reasoning) stage2Reasoning.textContent = s2Reasoning;
  if (stage2Content && s2Content) stage2Content.textContent = s2Content;
}

// 从 stage1/stage2 raw_response 中提取 reasoning_content 字符串
// raw_response 可能是：null / string / { content, reasoning_content, ... } dict
function _extractReasoningContent(resp) {
  if (resp == null) return '';
  if (typeof resp === 'string') return '';
  if (typeof resp === 'object') {
    const r = resp.reasoning_content;
    if (r == null) return '';
    return typeof r === 'string' ? r : JSON.stringify(r, null, 2);
  }
  return '';
}

// 从 stage1/stage2 raw_response 中提取 content 字符串
function _extractContent(resp) {
  if (resp == null) return '';
  if (typeof resp === 'string') return resp;
  if (typeof resp === 'object') {
    const c = resp.content;
    if (c == null) return '';
    return typeof c === 'string' ? c : JSON.stringify(c, null, 2);
  }
  return '';
}
