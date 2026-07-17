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
  const btnCancel = $('#btn-cancel');
  btn.disabled = true;
  btn.textContent = '分析中…';
  // 启用取消按钮
  btnCancel.disabled = false;

  // Clear panels
  $('#stream-reasoning').textContent = '';
  $('#stream-content').textContent = '';
  $('#stream-usage').textContent = '';
  $('#stage-badge').textContent = '';
  $('#decision-content').innerHTML = '';
  // 清空 prompt 展示区
  const promptDisp = $('#prompt-display');
  if (promptDisp) promptDisp.innerHTML = '';

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
              // 若后端附带 attempt 字段则显示重试次数，否则通用提示
              stage = evt.attempt != null
                ? `🔄 阶段一第 ${evt.attempt} 次重试…`
                : '🔄 阶段一重试中…';
              break;
            case 'Stage1Done':
              stage = '⏳ 构建阶段二…';
              break;
            case 'Stage1Failed':
              stage = '❌ 阶段一失败';
              break;
            case 'Stage2Started':
              stage = '🎯 阶段二：交易决策';
              break;
            case 'Stage2Retry':
              stage = evt.attempt != null
                ? `🔄 阶段二第 ${evt.attempt} 次重试…`
                : '🔄 阶段二重试中…';
              break;
            case 'Stage2Done':
              stage = '✅ 分析完成';
              break;
            case 'Stage2Failed':
              stage = '❌ 阶段二失败';
              break;
            case 'InsufficientData':
              stage = '⚠️ 数据不足，无法分析';
              break;
            case 'RecordSaved':
              // 不阻塞流程，仅在内容区追加提示
              $('#stream-content').textContent += '\n💾 记录已保存';
              break;
            case 'Cancelled':
              stage = '⚠️ 已取消';
              break;
            default:
              // 未知事件保持当前阶段
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
          // 阶段 prompt 全文展示到可折叠区域
          appendPromptBlock(evt.stage, evt.system, evt.user);
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
    // 禁用取消按钮
    btnCancel.disabled = true;
    currentAnalysisStream = null;
  }
}

function renderDecision(record) {
  // orchestrator 输出的 stage2_decision 本身即为决策对象（normalizer 已展平）
  const d = record.stage2_decision || {};
  const s1 = record.stage1_diagnosis || {};
  const orderType = d.order_type || '不下单';
  const direction = d.order_direction || '';
  const cls = direction === '做多' || direction === 'buy' ? 'buy' :
              direction === '做空' || direction === 'sell' ? 'sell' : '';

  // 不下单分支：隐藏全部价格字段，仅显示状态与 reasoning
  const isNoOrder = orderType === '不下单';

  let html = `<div class="decision-card ${cls}">`;
  html += `<h3>📊 交易决策</h3>`;
  html += `<div class="order-type">${escapeHtml(orderType)}${direction ? '（' + escapeHtml(direction) + '）' : ''}</div>`;

  if (isNoOrder) {
    // 本轮不下单：仅显示 reasoning
    if (d.reasoning) {
      html += `<div class="field"><span class="key">理由</span><span class="val">${escapeHtml(String(d.reasoning))}</span></div>`;
    }
  } else {
    // 价格相关字段
    if (d.entry_price != null) html += `<div class="field"><span class="key">入场价</span><span class="val">${escapeHtml(String(d.entry_price))}</span></div>`;
    if (d.stop_loss_price != null) html += `<div class="field"><span class="key">止损</span><span class="val">${escapeHtml(String(d.stop_loss_price))}</span></div>`;
    if (d.take_profit_price != null) html += `<div class="field"><span class="key">止盈 TP1</span><span class="val">${escapeHtml(String(d.take_profit_price))}</span></div>`;
    if (d.take_profit_price_2 != null) html += `<div class="field"><span class="key">止盈 TP2</span><span class="val">${escapeHtml(String(d.take_profit_price_2))}</span></div>`;
    if (d.entry_rule) html += `<div class="field"><span class="key">入场规则</span><span class="val">${escapeHtml(String(d.entry_rule))}</span></div>`;
    if (d.entry_basis_bar != null) html += `<div class="field"><span class="key">入场基准K线</span><span class="val">${escapeHtml(String(d.entry_basis_bar))}</span></div>`;
    if (d.entry_basis_extreme != null) html += `<div class="field"><span class="key">入场基准极值</span><span class="val">${escapeHtml(String(d.entry_basis_extreme))}</span></div>`;

    // 置信度与胜率进度条（0-100 整数）
    if (d.diagnosis_confidence != null) {
      html += renderConfidenceBar('诊断置信度', d.diagnosis_confidence);
    }
    if (d.trade_confidence != null) {
      html += renderConfidenceBar('交易置信度', d.trade_confidence);
    }
    if (d.estimated_win_rate != null) {
      html += renderConfidenceBar('预估胜率', d.estimated_win_rate);
    }

    // 风险评估、无效条件
    if (d.risk_assessment) html += `<div class="field"><span class="key">风险评估</span><span class="val">${escapeHtml(String(d.risk_assessment))}</span></div>`;
    if (d.invalidation_condition) html += `<div class="field"><span class="key">无效条件</span><span class="val">${escapeHtml(String(d.invalidation_condition))}</span></div>`;

    // 关键因素 / 关注点列表
    if (Array.isArray(d.key_factors) && d.key_factors.length) {
      html += renderListField('关键因素', d.key_factors);
    }
    if (Array.isArray(d.watch_points) && d.watch_points.length) {
      html += renderListField('关注点', d.watch_points);
    }

    // 决策逻辑
    if (d.reasoning) {
      html += `<div class="field"><span class="key">逻辑</span><span class="val">${escapeHtml(String(d.reasoning))}</span></div>`;
    }

    // 置信度/胜率说明文本
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

  // 阶段一诊断卡片（保留原有结构）
  if (s1.direction || s1.cycle_position) {
    html += `<div class="decision-card">`;
    html += `<h3>🔬 阶段一诊断</h3>`;
    if (s1.direction) html += `<div class="field"><span class="key">方向</span><span class="val">${escapeHtml(String(s1.direction))}</span></div>`;
    if (s1.cycle_position) html += `<div class="field"><span class="key">周期位置</span><span class="val">${escapeHtml(String(s1.cycle_position))}</span></div>`;
    if (s1.volatility_regime) html += `<div class="field"><span class="key">波动率</span><span class="val">${escapeHtml(String(s1.volatility_regime))}</span></div>`;
    html += `</div>`;
  }

  if (record.exception) {
    html += `<div class="decision-card status-err"><h3>⚠️ 异常</h3><pre>${escapeHtml(JSON.stringify(record.exception, null, 2))}</pre></div>`;
  }

  $('#decision-content').innerHTML = html;
}

// 渲染 0-100 置信度进度条
function renderConfidenceBar(label, value) {
  const v = Math.max(0, Math.min(100, parseInt(value, 10) || 0));
  return `<div class="confidence-row">
    <span class="key">${escapeHtml(label)}</span>
    <div class="confidence-bar"><div class="confidence-fill" style="width:${v}%"></div></div>
    <span class="val">${v}%</span>
  </div>`;
}

// 渲染列表型字段（关键因素 / 关注点）
function renderListField(label, items) {
  const lis = items.map(it => `<li>${escapeHtml(String(it))}</li>`).join('');
  return `<div class="list-field"><span class="key">${escapeHtml(label)}</span><ul>${lis}</ul></div>`;
}

// 追加一个可折叠的 prompt 展示块（system / user）到 #prompt-display
function appendPromptBlock(stage, system, user) {
  const container = $('#prompt-display');
  if (!container) return;
  const stageLabel = stage === 'stage2' ? '阶段二' : '阶段一';

  // 使用 <details> 实现原生可折叠，默认折叠
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
