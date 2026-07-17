// app.js — PA Agent Web main UI

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

// ── Cycle 8 keys 中文标签（与 pa_agent/ai/cycle_enums.py 对齐） ─────
const CYCLE_LABELS = {
  spike: '极速趋势',
  micro_channel: '微通道',
  tight_channel: '紧通道',
  normal_channel: '正常通道',
  broad_channel: '宽通道',
  trending_tr: '趋势中震荡',
  trading_range: '震荡区间',
  extreme_tr: '极端震荡',
};

// ── Globals ────────────────────────────────────────────────────────────
let chart, candleSeries, emaSeries;
let lastRecord = null;
let currentSettings = null;
let lastBars = null;           // 最近一次 /api/bars 返回的 K 线（供实时刷新复用）
let liveRefreshTimer = null;   // 实时刷新 setInterval 句柄
let liveRefreshLastTs = 0;     // 上次刷新时间戳（ms）

// ── Init ───────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  const { chart: c, candleSeries: cs, emaSeries: es } = createChart($('#chart-container'));
  chart = c; candleSeries = cs; emaSeries = es;

  await loadBars();
  await loadSettings();
  await loadDataSources();
  await loadTimeframes();

  bindEvents();
  resizeChart();
});

window.addEventListener('resize', resizeChart);

function resizeChart() {
  const el = $('#chart-container');
  if (chart) chart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
}

// ── Data loading ───────────────────────────────────────────────────────
async function loadBars() {
  try {
    const data = await API.get('/api/bars?count=100');
    lastBars = data.bars || [];
    setBars(candleSeries, lastBars);
    setEma(emaSeries, lastBars);
    setSeqMarkers(candleSeries, lastBars);
    // 记录最新一根 bar 的时间，供 setDirectionMarker 锚定
    if (lastBars.length) {
      const sorted = [...lastBars].sort((a, b) => a.ts_open - b.ts_open);
      window.__PA_LAST_BAR_TIME__ = sorted[sorted.length - 1].ts_open / 1000;
    }
    fitView(chart);
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
    setEma(emaSeries, lastBars);
    setSeqMarkers(candleSeries, lastBars);
    if (lastBars.length) {
      const sorted = [...lastBars].sort((a, b) => a.ts_open - b.ts_open);
      window.__PA_LAST_BAR_TIME__ = sorted[sorted.length - 1].ts_open / 1000;
    }
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
    // 飞书设置
    const fs = s.feishu || {};
    $('#s-feishu-enabled').checked = fs.enabled !== false;
    $('#s-feishu-webhook').value = fs.webhook_url || '';
    $('#s-feishu-secret').value = fs.secret || '';
    $('#s-feishu-app-id').value = fs.app_id || '';
    $('#s-feishu-app-secret').value = fs.app_secret || '';
    $('#s-feishu-order-only').checked = fs.notify_on_order_only !== false;
    return s;
  } catch (e) {
    console.error('loadSettings:', e);
  }
}

async function loadDataSources() {
  try {
    const list = await API.get('/api/datasources');
    const sel = $('#ds-kind');
    sel.innerHTML = list.map(d => `<option value="${d.id}">${d.label}</option>`).join('');
  } catch (e) {
    console.error('loadDataSources:', e);
  }
}

async function loadTimeframes() {
  try {
    const list = await API.get('/api/timeframes');
    const sel = $('#ds-timeframe');
    sel.innerHTML = list.map(t => `<option value="${t}">${t}</option>`).join('');
  } catch (e) {
    console.error('loadTimeframes:', e);
  }
}

// ── Events ─────────────────────────────────────────────────────────────
let currentAnalysisStream = null;

function bindEvents() {
  $('#btn-refresh').addEventListener('click', loadBars);
  $('#btn-analyze').addEventListener('click', startAnalysis);
  // 取消按钮：中止当前分析 SSE 流
  $('#btn-cancel').addEventListener('click', () => {
    if (currentAnalysisStream) {
      currentAnalysisStream.abort();
    }
  });
  $('#btn-settings').addEventListener('click', () => $('#settings-modal').classList.remove('hidden'));
  $('.modal-close').addEventListener('click', () => $('#settings-modal').classList.add('hidden'));
  $('#btn-modal-close').addEventListener('click', () => $('#settings-modal').classList.add('hidden'));
  $('#settings-modal').addEventListener('click', (e) => {
    if (e.target === $('#settings-modal')) $('#settings-modal').classList.add('hidden');
  });

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
    });
  });

  // Chat
  $('#btn-chat-send').addEventListener('click', sendChat);
  $('#chat-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') sendChat();
  });

  // 实时刷新开关
  $('#cb-live-refresh').addEventListener('change', (e) => {
    if (e.target.checked) startLiveRefresh();
    else stopLiveRefresh();
  });
}

async function saveSettingsHandler() {
  try {
    await API.put('/api/settings', {
      provider: {
        base_url: $('#s-base-url').value,
        model: $('#s-model').value,
        api_key: $('#s-api-key').value,
        reasoning_effort: $('#s-reasoning-effort').value,
        thinking: $('#s-thinking').checked,
      },
      general: {
        refresh_interval_ms: parseInt($('#s-refresh-ms').value) || 1000,
        decision_stance: $('#s-decision-stance').value,
        context_warning_threshold_pct: parseInt($('#s-ctx-warn').value) || 80,
        analysis_bar_count: parseInt($('#ds-bar-count').value) || 100,
      },
      feishu: {
        enabled: $('#s-feishu-enabled').checked,
        webhook_url: $('#s-feishu-webhook').value,
        secret: $('#s-feishu-secret').value,
        app_id: $('#s-feishu-app-id').value,
        app_secret: $('#s-feishu-app-secret').value,
        notify_on_order_only: $('#s-feishu-order-only').checked,
      },
    });
    $('#settings-modal').classList.add('hidden');
    alert('设置已保存');
    // 重新加载以同步 currentSettings 与 context_window
    await loadSettings();
    // 若实时刷新已开启，重启 timer 以应用新的 refresh_interval_ms
    if ($('#cb-live-refresh').checked) {
      stopLiveRefresh();
      startLiveRefresh();
    }
  } catch (e) {
    alert('保存失败: ' + e.message);
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

// ── 实时刷新 ──────────────────────────────────────────────────────────
function startLiveRefresh() {
  stopLiveRefresh();
  const interval = parseInt($('#s-refresh-ms').value) || 1000;
  // 限制最小 500ms，避免压垮后端
  const safeInterval = Math.max(500, interval);
  liveRefreshTimer = setInterval(refreshBarsOnly, safeInterval);
  liveRefreshLastTs = Date.now();
  updateLiveRefreshStatus();
}

function stopLiveRefresh() {
  if (liveRefreshTimer) {
    clearInterval(liveRefreshTimer);
    liveRefreshTimer = null;
  }
  updateLiveRefreshStatus();
}

function updateLiveRefreshStatus() {
  const el = $('#live-refresh-status');
  if (!el) return;
  if (!liveRefreshTimer) {
    el.textContent = '';
    return;
  }
  const elapsed = liveRefreshLastTs ? Math.max(0, Date.now() - liveRefreshLastTs) : 0;
  el.textContent = `· 距上次刷新 ${(elapsed / 1000).toFixed(1)}s`;
}

// 每秒更新一次"距上次刷新"显示
setInterval(updateLiveRefreshStatus, 1000);

// ── Analysis ───────────────────────────────────────────────────────────
async function startAnalysis() {
  const btn = $('#btn-analyze');
  const btnCancel = $('#btn-cancel');
  btn.disabled = true;
  btn.textContent = '分析中…';
  btnCancel.disabled = false;

  // Clear panels
  $('#stream-reasoning').textContent = '';
  $('#stream-content').textContent = '';
  $('#stream-usage').textContent = '';
  $('#stage-badge').textContent = '';
  $('#decision-content').innerHTML = '';
  $('#future-content').innerHTML = '分析中…';
  $('#tree-content').innerHTML = '分析中…';
  // 重置 token 进度条
  updateTokenProgress(null);
  // 清空 prompt 展示区
  const promptDisp = $('#prompt-display');
  if (promptDisp) promptDisp.innerHTML = '';
  // 清空图表叠加层（保留 EMA 与 K 线）
  clearOverlays(candleSeries);
  setSeqMarkers(candleSeries, lastBars || []);

  const barCount = parseInt($('#ds-bar-count').value) || 100;

  try {
    const { controller, source } = API.sse(`/api/analyze/stream?bar_count=${barCount}`);
    currentAnalysisStream = controller;

    let stage = '';
    for await (const evt of source) {
      switch (evt.type) {
        case 'orchestrator_event':
          // 处理 orchestrator 全部 11 个事件
          switch (evt.event) {
            case 'Stage1Started':
              stage = '🔍 阶段一：市场诊断';
              break;
            case 'Stage1Retry':
              stage = evt.attempt != null
                ? `🔄 阶段一第 ${evt.attempt} 次重试…`
                : '🔄 阶段一重试中…';
              if (evt.reason) stage += `（${evt.reason}）`;
              break;
            case 'Stage1Done':
              stage = '⏳ 构建阶段二…';
              break;
            case 'Stage1Failed':
              stage = '❌ 阶段一失败';
              if (evt.reason) stage += `：${evt.reason}`;
              break;
            case 'Stage2Started':
              stage = '🎯 阶段二：交易决策';
              break;
            case 'Stage2Retry':
              stage = evt.attempt != null
                ? `🔄 阶段二第 ${evt.attempt} 次重试…`
                : '🔄 阶段二重试中…';
              if (evt.reason) stage += `（${evt.reason}）`;
              break;
            case 'Stage2Done':
              stage = '✅ 分析完成';
              break;
            case 'Stage2Failed':
              stage = '❌ 阶段二失败';
              if (evt.reason) stage += `：${evt.reason}`;
              break;
            case 'InsufficientData':
              stage = '⚠️ 数据不足，无法分析';
              if (evt.reason) stage += `：${evt.reason}`;
              break;
            case 'RecordSaved':
              $('#stream-content').textContent += '\n💾 记录已保存';
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
          $('#stream-reasoning').textContent += evt.chunk;
          $('#stream-reasoning').scrollTop = $('#stream-reasoning').scrollHeight;
          break;
        case 'content_token':
          $('#stream-content').textContent += evt.chunk;
          $('#stream-content').scrollTop = $('#stream-content').scrollHeight;
          break;
        case 'stage_prompt':
          appendPromptBlock(evt.stage, evt.system, evt.user);
          break;
        case 'strategy_files':
          // 可选：展示命中的策略文件
          if (Array.isArray(evt.files) && evt.files.length) {
            $('#stream-content').textContent += `\n📑 策略文件：${evt.files.join(', ')}`;
          }
          break;
        case 'done': {
          lastRecord = evt.record;
          renderDecision(evt.record);
          renderFuturePanel(evt.record);
          renderDecisionTree(evt.record);
          renderTokenUsage(evt.record.usage_total);
          updateTokenProgress(evt.record.usage_total);
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
          fitView(chart);
          enableChat();
          break;
        }
        case 'error':
          $('#stage-badge').textContent = `❌ 错误: ${evt.message}`;
          $('#stream-content').textContent += `\n[错误] ${evt.message}`;
          break;
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') {
      $('#stage-badge').textContent = `❌ 连接错误: ${e.message}`;
    }
  } finally {
    btn.disabled = false;
    btn.textContent = '分析';
    btnCancel.disabled = true;
    currentAnalysisStream = null;
  }
}

// ── 决策卡片渲染 ──────────────────────────────────────────────────────
function renderDecision(record) {
  const d = record.stage2_decision || {};
  const s1 = record.stage1_diagnosis || {};
  const orderType = d.order_type || '不下单';
  const direction = d.order_direction || '';
  const cls = direction === '做多' || direction === 'buy' || direction === 'long' ? 'buy' :
              direction === '做空' || direction === 'sell' || direction === 'short' ? 'sell' : '';

  const isNoOrder = orderType === '不下单' || orderType === 'no_order';

  let html = `<div class="decision-card ${cls}">`;
  html += `<h3>📊 交易决策</h3>`;
  html += `<div class="order-type">${escapeHtml(orderType)}${direction ? '（' + escapeHtml(direction) + '）' : ''}</div>`;

  if (isNoOrder) {
    if (d.reasoning) {
      html += `<div class="field"><span class="key">理由</span><span class="val">${escapeHtml(String(d.reasoning))}</span></div>`;
    }
  } else {
    if (d.entry_price != null) html += `<div class="field"><span class="key">入场价</span><span class="val">${escapeHtml(String(d.entry_price))}</span></div>`;
    if (d.stop_loss_price != null) html += `<div class="field"><span class="key">止损</span><span class="val">${escapeHtml(String(d.stop_loss_price))}</span></div>`;
    if (d.take_profit_price != null) html += `<div class="field"><span class="key">止盈 TP1</span><span class="val">${escapeHtml(String(d.take_profit_price))}</span></div>`;
    if (d.take_profit_price_2 != null) html += `<div class="field"><span class="key">止盈 TP2</span><span class="val">${escapeHtml(String(d.take_profit_price_2))}</span></div>`;
    if (d.entry_rule) html += `<div class="field"><span class="key">入场规则</span><span class="val">${escapeHtml(String(d.entry_rule))}</span></div>`;
    if (d.entry_basis_bar != null) html += `<div class="field"><span class="key">入场基准K线</span><span class="val">${escapeHtml(String(d.entry_basis_bar))}</span></div>`;
    if (d.entry_basis_extreme != null) html += `<div class="field"><span class="key">入场基准极值</span><span class="val">${escapeHtml(String(d.entry_basis_extreme))}</span></div>`;

    // 盈亏比 + trader equation
    const rr = computeRiskReward(d.entry_price, d.take_profit_price, d.stop_loss_price, direction);
    if (rr) {
      html += `<div class="field"><span class="key">盈亏比</span><span class="val">${escapeHtml(rr.ratioText)}</span></div>`;
      // trader equation 判定
      const winRate = parseWinRate(d.estimated_win_rate);
      if (winRate != null && rr.risk > 0 && rr.reward > 0) {
        const passes = (winRate / 100) * rr.reward >= ((100 - winRate) / 100) * rr.risk;
        html += `<div class="field"><span class="key">交易员方程</span><span class="val ${passes ? 'status-ok' : 'status-err'}">${passes ? '✓ 通过' : '✗ 不通过'}</span></div>`;
      }
    }

    // 置信度与胜率进度条
    if (d.diagnosis_confidence != null) {
      html += renderConfidenceBar('诊断置信度', d.diagnosis_confidence);
    }
    if (d.trade_confidence != null) {
      html += renderConfidenceBar('交易置信度', d.trade_confidence);
    }
    if (d.estimated_win_rate != null) {
      html += renderConfidenceBar('预估胜率', d.estimated_win_rate);
    }

    if (d.risk_assessment) html += `<div class="field"><span class="key">风险评估</span><span class="val">${escapeHtml(String(d.risk_assessment))}</span></div>`;
    if (d.invalidation_condition) html += `<div class="field"><span class="key">无效条件</span><span class="val">${escapeHtml(String(d.invalidation_condition))}</span></div>`;

    if (Array.isArray(d.key_factors) && d.key_factors.length) {
      html += renderListField('关键因素', d.key_factors);
    }
    if (Array.isArray(d.watch_points) && d.watch_points.length) {
      html += renderListField('关注点', d.watch_points);
    }

    if (d.reasoning) {
      html += `<div class="field"><span class="key">逻辑</span><span class="val">${escapeHtml(String(d.reasoning))}</span></div>`;
    }
    if (d.diagnosis_confidence_reasoning) {
      html += `<div class="field"><span class="key">诊断置信度说明</span><span class="val">${escapeHtml(String(d.diagnosis_confidence_reasoning))}</span></div>`;
    }
    if (d.trade_confidence_reasoning) {
      html += `<div class="field"><span class="key">交易置信度说明</span><span class="val">${escapeHtml(String(d.trade_confidence_reasoning))}</span></div>`;
    }
    if (d.estimated_win_rate_reasoning) {
      html += `<div class="field"><span class="key">胜率说明</span><span class="val">${escapeHtml(String(d.estimated_win_rate_reasoning))}</span></div>`;
    }
  }
  html += `</div>`;

  // 阶段一诊断卡片
  if (s1.direction || s1.cycle_position) {
    html += `<div class="decision-card">`;
    html += `<h3>🔬 阶段一诊断</h3>`;
    if (s1.direction) html += `<div class="field"><span class="key">方向</span><span class="val">${escapeHtml(String(s1.direction))}</span></div>`;
    if (s1.cycle_position) html += `<div class="field"><span class="key">周期位置</span><span class="val">${escapeHtml(String(s1.cycle_position))}</span></div>`;
    if (s1.alternative_cycle_position) html += `<div class="field"><span class="key">备选周期</span><span class="val">${escapeHtml(String(s1.alternative_cycle_position))}</span></div>`;
    if (s1.market_phase) html += `<div class="field"><span class="key">市场阶段</span><span class="val">${escapeHtml(String(s1.market_phase))}</span></div>`;
    if (s1.transition_risk) html += `<div class="field"><span class="key">过渡风险</span><span class="val">${escapeHtml(String(s1.transition_risk))}</span></div>`;
    if (s1.volatility_regime) html += `<div class="field"><span class="key">波动率</span><span class="val">${escapeHtml(String(s1.volatility_regime))}</span></div>`;
    // 支撑/阻力位文本展示
    const srText = formatSupportResistanceText(s1);
    if (srText) html += `<div class="field"><span class="key">支撑/阻力</span><span class="val">${escapeHtml(srText)}</span></div>`;
    if (s1.gate_result) html += `<div class="field"><span class="key">闸门结果</span><span class="val">${escapeHtml(String(s1.gate_result))}</span></div>`;
    html += `</div>`;
  }

  if (record.exception) {
    html += `<div class="decision-card status-err"><h3>⚠️ 异常</h3><pre>${escapeHtml(JSON.stringify(record.exception, null, 2))}</pre></div>`;
  }

  $('#decision-content').innerHTML = html;
}

// 渲染 0-100 置信度进度条
function renderConfidenceBar(label, value) {
  const v = parsePercent(value);
  return `<div class="confidence-row">
    <span class="key">${escapeHtml(label)}</span>
    <div class="confidence-bar"><div class="confidence-fill" style="width:${v}%"></div></div>
    <span class="val">${v}%</span>
  </div>`;
}

function renderListField(label, items) {
  const lis = items.map(it => `<li>${escapeHtml(String(it))}</li>`).join('');
  return `<div class="list-field"><span class="key">${escapeHtml(label)}</span><ul>${lis}</ul></div>`;
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
    html += '</div>';
    return html;
  }
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
  if (pred.reasoning) {
    html += `<div class="future-reasoning">${escapeHtml(String(pred.reasoning))}</div>`;
  }
  html += '</div>';
  return html;
}

function renderNextCyclePrediction(pred) {
  if (!pred) return '';
  let html = '<div class="future-section"><h3>🔄 下一个市场周期预期</h3>';
  if (pred.unpredictable) {
    html += '<div class="future-dir unknown">不可预测</div>';
    html += '<div class="future-reasoning">市场处于过渡或混乱状态，无法给出周期概率</div>';
    html += '</div>';
    return html;
  }
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
  if (pred.reasoning) {
    html += `<div class="future-reasoning">${escapeHtml(String(pred.reasoning))}</div>`;
  }
  html += '</div>';
  return html;
}

function probChip(label, value, isTop) {
  const v = Math.round(value) || 0;
  return `<span class="future-prob-chip${isTop ? ' top' : ''}">${escapeHtml(label)} ${v}%</span>`;
}

// ── 决策树路径回放表格 ────────────────────────────────────────────────
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

  // 6 列表格：步 / 阶段 / 节点 / 回答 / K线依据 / 理由
  html += '<div class="tree-table-wrap"><table class="tree-table"><thead><tr>';
  html += '<th>步</th><th>阶段</th><th>节点</th><th>回答</th><th>K线依据</th><th>理由</th>';
  html += '</tr></thead><tbody>';
  merged.forEach((item, i) => {
    const phase = String(item.phase || '').toLowerCase();
    const phaseZh = phase === 'gate' ? '闸门' : phase === 'decision' ? '策略' : phase;
    const answerInfo = formatTraceAnswer(item);
    const barBasis = normalizeBarRange(item);
    const reason = item.reason || '';
    const skipped = item.skipped === true;
    html += `<tr class="${skipped ? 'skipped' : ''}">`;
    html += `<td>${i + 1}</td>`;
    html += `<td>${escapeHtml(phaseZh)}</td>`;
    html += `<td>${escapeHtml(String(item.node_id || ''))}</td>`;
    html += `<td class="${answerInfo.cls}">${escapeHtml(answerInfo.text)}</td>`;
    html += `<td>${escapeHtml(barBasis)}</td>`;
    html += `<td class="reason-cell">${escapeHtml(String(reason))}</td>`;
    html += '</tr>';
  });
  html += '</tbody></table></div>';
  el.innerHTML = html;
}

function mergeTraces(gate, decision) {
  // 保持顺序：先 gate 后 decision（与 PyQt6 行为一致）
  return [...gate, ...decision];
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

function normalizeBarRange(item) {
  // 优先 item.bar_basis / item.basis_bars / item.kline_basis
  const v = item.bar_basis || item.basis_bars || item.kline_basis || item.bars;
  if (v == null || v === '') return '';
  if (typeof v === 'string') return v;
  if (Array.isArray(v)) return v.join(',');
  if (typeof v === 'object') {
    // 可能是 {from: n, to: m}
    if ('from' in v && 'to' in v) return `${v.from}-${v.to}`;
    return JSON.stringify(v);
  }
  return String(v);
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

// ── Prompt 展示（可折叠） ─────────────────────────────────────────────
function appendPromptBlock(stage, system, user) {
  const container = $('#prompt-display');
  if (!container) return;
  const stageLabel = stage === 'stage2' ? '阶段二' : '阶段一';

  const details = document.createElement('details');
  details.className = 'prompt-block';

  const summary = document.createElement('summary');
  summary.textContent = `📝 ${stageLabel} Prompt`;
  details.appendChild(summary);

  if (system) {
    const sysDiv = document.createElement('div');
    sysDiv.className = 'prompt-section';
    sysDiv.innerHTML = `<div class="prompt-section-title">System</div><pre class="prompt-text">${escapeHtml(system)}</pre>`;
    details.appendChild(sysDiv);
  }
  if (user) {
    const userDiv = document.createElement('div');
    userDiv.className = 'prompt-section';
    userDiv.innerHTML = `<div class="prompt-section-title">User</div><pre class="prompt-text">${escapeHtml(user)}</pre>`;
    details.appendChild(userDiv);
  }

  container.appendChild(details);
}

// ── Chat ───────────────────────────────────────────────────────────────
function enableChat() {
  $('#chat-input').disabled = false;
  $('#btn-chat-send').disabled = false;
}

let chatStreamCtrl = null;

async function sendChat() {
  const input = $('#chat-input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  input.disabled = true;
  $('#btn-chat-send').disabled = true;

  appendChatMsg('user', text);

  const recordId = lastRecord ? (
    `${lastRecord.symbol}_${lastRecord.timeframe}_${Date.now()}`
  ) : 'latest';

  try {
    const { controller, source } = API.sse(`/api/chat/stream?text=${encodeURIComponent(text)}&record_id=${encodeURIComponent(recordId)}`);
    chatStreamCtrl = controller;

    let reasoning = '', content = '';
    let msgDiv = appendChatMsg('assistant', '');

    for await (const evt of source) {
      if (evt.type === 'reasoning_token') {
        reasoning += evt.chunk;
        const rDiv = msgDiv.querySelector('.reasoning');
        if (rDiv) rDiv.textContent = reasoning;
      } else if (evt.type === 'content_token') {
        content += evt.chunk;
        msgDiv.querySelector('.bubble').textContent = content;
        $('#chat-history').scrollTop = $('#chat-history').scrollHeight;
      } else if (evt.type === 'done') {
        if (evt.reasoning) msgDiv.querySelector('.reasoning').textContent = evt.reasoning;
        if (evt.content) msgDiv.querySelector('.bubble').textContent = evt.content;
      } else if (evt.type === 'error') {
        msgDiv.querySelector('.bubble').textContent = '[错误] ' + evt.message;
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') {
      appendChatMsg('assistant', '[错误] ' + e.message);
    }
  } finally {
    input.disabled = false;
    $('#btn-chat-send').disabled = false;
    chatStreamCtrl = null;
  }
}

function appendChatMsg(role, text) {
  const div = document.createElement('div');
  div.className = `chat-msg ${role}`;
  if (role === 'assistant') {
    div.innerHTML = `<div class="reasoning"></div><div class="bubble">${escapeHtml(text)}</div>`;
  } else {
    div.innerHTML = `<div class="bubble">${escapeHtml(text)}</div>`;
  }
  $('#chat-history').appendChild(div);
  $('#chat-history').scrollTop = $('#chat-history').scrollHeight;
  return div;
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
