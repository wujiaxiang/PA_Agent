// app.js — PA Agent Web main UI

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

// ── Globals ────────────────────────────────────────────────────────────
let chart, candleSeries;
let lastRecord = null;

// ── Init ───────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  const { chart: c, candleSeries: cs } = createChart($('#chart-container'));
  chart = c; candleSeries = cs;

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
    setBars(candleSeries, data.bars);
  } catch (e) {
    console.error('loadBars:', e);
  }
}

async function loadSettings() {
  try {
    const s = await API.get('/api/settings');
    $('#s-base-url').value = s.provider?.base_url || '';
    $('#s-model').value = s.provider?.model || '';
    $('#s-api-key').value = s.provider?.api_key || '';
    $('#s-reasoning-effort').value = s.provider?.reasoning_effort || 'high';
    $('#s-thinking').checked = s.provider?.thinking !== false;
    $('#s-refresh-ms').value = s.general?.refresh_interval_ms || 1000;
    $('#s-decision-stance').value = s.general?.decision_stance || 'balanced';
    $('#s-ctx-warn').value = s.general?.context_warning_threshold_pct || 80;
    $('#ds-bar-count').value = s.general?.analysis_bar_count || 100;
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
    });
    $('#settings-modal').classList.add('hidden');
    alert('设置已保存');
  } catch (e) {
    alert('保存失败: ' + e.message);
  }
}

// ── Analysis ───────────────────────────────────────────────────────────
async function startAnalysis() {
  const btn = $('#btn-analyze');
  btn.disabled = true;
  btn.textContent = '分析中…';

  // Clear panels
  $('#stream-reasoning').textContent = '';
  $('#stream-content').textContent = '';
  $('#stream-usage').textContent = '';
  $('#stage-badge').textContent = '';
  $('#decision-content').innerHTML = '';

  const barCount = parseInt($('#ds-bar-count').value) || 100;

  try {
    const { controller, source } = API.sse(`/api/analyze/stream?bar_count=${barCount}`);
    currentAnalysisStream = controller;

    let stage = '';
    for await (const evt of source) {
      switch (evt.type) {
        case 'orchestrator_event':
          if (evt.event === 'Stage1Started') stage = '🔍 阶段一：市场诊断';
          else if (evt.event === 'Stage1Done') stage = '⏳ 构建阶段二…';
          else if (evt.event === 'Stage2Started') stage = '🎯 阶段二：交易决策';
          else if (evt.event === 'Stage2Done') stage = '✅ 分析完成';
          else if (evt.event === 'Cancelled') stage = '⚠️ 已取消';
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
        case 'done':
          lastRecord = evt.record;
          renderDecision(evt.record);
          renderTokenUsage(evt.record.usage_total);
          enableChat();
          break;
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
    currentAnalysisStream = null;
  }
}

function renderDecision(record) {
  const d = record.stage2_decision?.decision || {};
  const s1 = record.stage1_diagnosis || {};
  const orderType = d.order_type || '不下单';
  const direction = d.order_direction || '';
  const cls = direction === '做多' || direction === 'buy' ? 'buy' :
              direction === '做空' || direction === 'sell' ? 'sell' : '';

  let html = `<div class="decision-card ${cls}">`;
  html += `<h3>📊 交易决策</h3>`;
  html += `<div class="order-type">${orderType}${direction ? '（' + direction + '）' : ''}</div>`;
  if (d.entry_price) html += `<div class="field"><span class="key">入场价</span><span class="val">${d.entry_price}</span></div>`;
  if (d.stop_loss) html += `<div class="field"><span class="key">止损</span><span class="val">${d.stop_loss}</span></div>`;
  if (d.take_profit) html += `<div class="field"><span class="key">止盈</span><span class="val">${d.take_profit}</span></div>`;
  if (d.confidence != null) html += `<div class="field"><span class="key">置信度</span><span class="val">${d.confidence}</span></div>`;
  if (d.reasoning) html += `<div class="field"><span class="key">逻辑</span><span class="val">${d.reasoning}</span></div>`;
  html += `</div>`;

  if (s1.direction || s1.cycle_position) {
    html += `<div class="decision-card">`;
    html += `<h3>🔬 阶段一诊断</h3>`;
    if (s1.direction) html += `<div class="field"><span class="key">方向</span><span class="val">${s1.direction}</span></div>`;
    if (s1.cycle_position) html += `<div class="field"><span class="key">周期位置</span><span class="val">${s1.cycle_position}</span></div>`;
    if (s1.volatility_regime) html += `<div class="field"><span class="key">波动率</span><span class="val">${s1.volatility_regime}</span></div>`;
    html += `</div>`;
  }

  if (record.exception) {
    html += `<div class="decision-card status-err"><h3>⚠️ 异常</h3><pre>${JSON.stringify(record.exception, null, 2)}</pre></div>`;
  }

  $('#decision-content').innerHTML = html;
}

function renderTokenUsage(usage) {
  if (!usage) return;
  $('#stream-usage').textContent = `Token: prompt=${usage.prompt_tokens || 0} completion=${usage.completion_tokens || 0} total=${usage.total_tokens || 0}`;
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
