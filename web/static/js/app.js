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
// 回看历史时暂存的「实时订阅」，供「返回实时」恢复。
// 此前回看只重渲染侧边栏、完全不碰图表，图上仍是当前品种，
// 且切到别的品种的历史记录时 K 线与决策价线完全对不上。
let _liveSubBeforeReplay = null;
let currentSettings = null;
let lastBars = null;           // 最近一次 /api/bars 返回的 K 线（供实时刷新复用）
// ── K 线实时流：前端按「自己」的游标轮询 ──────────────────────────────────────
// 原实现是服务端 SSE 全局广播（后台 Task 从 app.state.ctx 拉一次 K 线推给所有
// 连接），两个标签页看到的是同一条流，与「每个标签页服务自己的 K 线图」冲突；
// 「按游标分组广播」也被否决 —— 浏览器原生 EventSource 无法设置请求头，服务端
// 拿不到 X-Session-Id。改为轮询后，隐藏标签页自动停（visibilitychange 已处理），
// 一个坏 tab 不会传染其它 tab。
const BARS_POLL_INTERVAL_MS = 5000;      // /api/bars 轮询间隔，对齐原服务端 SSE 推送节奏
const KEEP_ANALYSIS_TICK_MS = 3000;      // 持续分析本地定时器 tick（纯本地判定，不发请求）

let liveRefreshTimer = null;   // 实时刷新 setInterval 句柄（K 线轮询）
let liveRefreshLastTs = 0;     // 上次刷新时间戳（ms）
let barsStreamPolling = false; // 「实时 K 线流」是否处于轮询活跃状态
let sseNextCloseTs = 0;        // 当前 forming bar 的下一收盘时间戳（ms，来自 /api/bars/next-close）
let marketClosed = false;      // /api/bars/next-close 上报的休市标志（bars[0].closed == true）
let keepAnalysisLastClosedTs = 0;  // 持续分析哨兵：上次处理的收盘 bar ts_open，防止同一根 bar 重复触发分析
let keepAnalysisTimer = null;  // 持续分析本地定时器句柄（替代原 bar_close SSE 事件）
// 下单机会订单类型 —— 由 /api/order-opportunity-types 从后端单一真源
// （pa_agent.ai.order_opportunity.ORDER_OPPORTUNITY_TYPES）拉取，避免前后端枚举漂移。
let ORDER_OPPORTUNITY_TYPES = ['限价单', '突破单', '市价单'];
// 删除历史记录的二次确认状态（不用 confirm()，见 deleteRecord 注释）
let pendingDeleteId = null;
let chartUpdatePaused = false;  // 分析期间暂停图表实时更新
let sseStatusExpiryTimer = null;  // updateSSEStatusWithExpiry 定时器句柄
let nextClosePollingTimer = null;  // 低频拉取 next_close_ts 定时器（轮询模式下使用）
let nextClosePollingInFlight = false;  // 防止并发 fetch
let displayTimezone = "Asia/Shanghai";  // 显示时区（IANA 名称），与 chart.js _displayTimezone 同步
let isAnalyzing = false;                // 是否有分析进行中（供「持续分析」开关判断）
let waitCloseCountdownTimer = null;     // 等待收盘 setInterval 句柄（仅轮询流不活跃时使用）
let waitCloseDisplayTimer = null;       // 等待收盘显示用 setInterval 句柄（勾选复选框时使用）
let waitCloseCountdownResolver = null;  // 轮询流活跃时倒计时归零的 resolve 回调（由 updateSSEStatusWithExpiry 触发）

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

// ── 切换性能重构（switch-performance-refactor spec） ──────────────────
// _inflightSwitch: 切换进行中标志位，防止 applySubscribe 重入
// _switchErrorTimer: showSwitchError 自动隐藏定时器
// symbolListCache / symbolListCacheTs: 品种列表缓存（exchange -> 数据 / 时间戳）
// _inflightExchange: 防止并发请求同一交易所品种列表
let _inflightSwitch = false;
let _switchErrorTimer = null;
const symbolListCache = new Map();
const symbolListCacheTs = new Map();
const SYMBOL_CACHE_TTL = 10 * 60 * 1000;  // 10 分钟
let _inflightExchange = null;

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

  // 兜底：用 ResizeObserver 监听 chart-pane 尺寸变化（处理折叠/展开、窗口分屏等场景）
  const chartPane = $('#chart-pane');
  if (chartPane && typeof ResizeObserver !== 'undefined') {
    const ro = new ResizeObserver(() => {
      if (typeof resizeChart === 'function') resizeChart();
    });
    ro.observe(chartPane);
  }
}

// ═══ 登录闸门 ═════════════════════════════════════════════════════════════
//
// **位置是这条约束的全部**：initLoginGate() 是 DOMContentLoaded 的第一件事，
// 早于 createChart、早于 bindEvents、早于 loadSettings → loadBars。
// 反过来做（先 boot、失败再补救）会付出三重代价：
//   1. boot 是 7 个 await 的链，loadSettings 一失败后面全不执行，只能事后判断；
//   2. 每一跳失败都会 showToast 一次，未登录时打出一片红字，用户看到的
//      是「系统坏了」而不是「请登录」；
//   3. 后端一旦把 ALLOW_ANONYMOUS_ADMIN 翻成 False，未登录页面上的每个请求都
//      是一次注定失败的往返 —— 既拖慢首屏，又在服务端日志里刷出一片 401。
//
// 与既有约束「bindEvents() 必须在数据加载前调用」同源：**可交互性不依赖数据
// 加载成功**。登录表单的 handler 在闸门内部当场绑定，闸门自己失败也不影响
// 用户点「登录」。

/** 登录页元素。集中取一次，避免各处散着 querySelector（id 拼错只在运行期炸）。 */
function _loginEls() {
  return {
    screen: $('#login-screen'),
    form: $('#login-form'),
    user: $('#login-username'),
    pass: $('#login-password'),
    submit: $('#login-submit'),
    error: $('#login-error'),
    status: $('#login-status'),
    led: $('#login-led'),
  };
}

const LOGIN_HINTS = {
  missing: '请使用管理员凭据登录',
  expired: '登录已过期，请重新登录',
  rejected: '会话已失效，请重新登录',
  loggedOut: '已登出',
  network: '无法连接服务端',
};

/** 切换到登录页。reason 只影响文案；tone 决定 LED 颜色（'error' 才是红）。 */
function showLoginScreen(reason, tone) {
  const el = _loginEls();
  PAuth.sessionDead = true;
  document.body.dataset.auth = 'login';
  const badge = $('#auth-user');
  if (badge) badge.textContent = '—';
  if (el.screen) el.screen.hidden = false;
  if (el.led) el.led.dataset.tone = tone === 'error' ? 'error' : (tone || 'idle');
  if (el.status && !tone) el.status.textContent = LOGIN_HINTS[reason] || LOGIN_HINTS.missing;
}

function setLoginError(message) {
  const el = _loginEls();
  if (!el.error) { if (message) showToast(message, 'error', { force: true }); return; }
  el.error.hidden = !message;
  el.error.textContent = message || '';
  if (el.led) el.led.dataset.tone = message ? 'error' : 'idle';
}

function setLoginBusy(busy, text) {
  const el = _loginEls();
  if (el.submit) {
    el.submit.disabled = !!busy;
    el.submit.dataset.busy = busy ? '1' : '';
  }
  if (el.led) el.led.dataset.tone = busy ? 'busy' : (el.error && el.error.hidden ? 'idle' : 'error');
  if (el.status && text) el.status.textContent = text;
}

/** 从 login / me 的响应里取展示名。
 *
 * 兼容三种形状，因为后端给的是嵌套 user，且 /auth/me 与 login 的字段略有出入：
 *   {display_name, user_id}  ← /api/auth/me 顶层
 *   {user: {display_name, user_id}}  ← POST /api/auth/login
 *   {username, user_id}             ← 容错
 * 取不到一律返回空串，**不猜、不留 undefined**。 */
function _displayNameOf(payload) {
  if (!payload || typeof payload !== 'object') return '';
  const u = (payload.user && typeof payload.user === 'object') ? payload.user : payload;
  return u.display_name || u.username || u.user_id || '';
}

/** 登录成功后回填顶栏的用户名。display 优先用 /auth/me 里的 display_name，
 *  拿不到就退回令牌载荷里的 sub —— 不猜、不留空。 */
function setAuthBadge(display) {
  const badge = $('#auth-user');
  if (badge) badge.textContent = display || PAuth.currentUserId() || '—';
}

/** 登录提交。**直接用 fetch 而不是 API.post**：需要区分 401（密码错）/
 *  404（后端端点未落地）/ 422（返回体形状对不上）三种完全不同的处置，
 *  而 API.post 只抛一个 message 字符串。/api/auth/* 本身也在闸门的免鉴权名单里，
 *  绕过 API.post 不会削弱任何鉴权检查。 */
async function handleLoginSubmit(ev) {
  if (ev) ev.preventDefault();
  const el = _loginEls();
  const username = (el.user && el.user.value || '').trim();
  const password = (el.pass && el.pass.value) || '';
  setLoginError('');
  if (!username) { setLoginError('请输入用户名'); if (el.user) el.user.focus(); return; }
  if (!password) { setLoginError('请输入密码'); if (el.pass) el.pass.focus(); return; }

  setLoginBusy(true, '校验中…');
  try {
    const headers = await sessionHeadersAsync({ 'Content-Type': 'application/json' });
    // ⚠️ 字段名 **user_id** 不是 username —— 与后端 routes_auth.py::LoginRequest
    // 逐字对齐（也与 users.user_id、authenticate() 同名）。写成 username 会被
    // pydantic 判成 422「请求畸形」，而 422 与 401 的区别在登录语境下毫无意义，
    // 用户只会看到一句看不懂的报错。
    const r = await fetch('/api/auth/login', {
      method: 'POST', headers, cache: 'no-cache',
      body: JSON.stringify({ user_id: username, password }),
    });
    if (r.status === 404) {
      setLoginError('登录接口未就绪（404）：后端 /api/auth/login 尚未落地');
      return;
    }
    if (r.status === 401 || r.status === 403) {
      setLoginError('用户名或密码错误');
      if (el.pass) { el.pass.value = ''; el.pass.focus(); }
      return;
    }
    if (!r.ok) {
      setLoginError(`登录失败（HTTP ${r.status}）`);
      return;
    }
    let data = null;
    try { data = await r.json(); } catch (_) { /* 落到下面的缺字段提示 */ }
    // 后端 routes_auth.py 返回的就是 `token`；`access_token` 只是容错别名。
    const token = (data && (data.token || data.access_token)) || '';
    if (!token) {
      setLoginError('响应里没有 token 字段（需为 {"token": "v1...."}）');
      return;
    }
    // 只挡「空串」。「格式对不对」交给服务端：前端把一个自己看不懂、
    // 但服务端认的令牌拒掉，等于凭空造出一个用户解不开的死局。
    if (!PAuth.hasToken(token)) {
      setLoginError('响应里没有 token 字段（需为 {"token": "v1...."}）');
      return;
    }
    PAuth.setAuthToken(token);
    setAuthBadge(_displayNameOf(data) || PAuth.currentUserId());
    showToast('登录成功，正在进入控制台…', 'success', { force: true });
    // 登录成功后**整页重载**，而不是就地启动主界面（取舍见交付说明）。
    // 重载保证主界面只可能由「一次通过了闸门的 boot」产生 —— 这条不变量
    // 正是「未登录绝不发业务请求」的结构性保证。
    location.reload();
  } catch (e) {
    setLoginError(`无法连接服务端：${(e && e.message) || e}`);
  } finally {
    setLoginBusy(false);
  }
}

/** 登出：清 token + 丢会话身份 + 重载。 */
async function doLogout() {
  const btn = $('#btn-logout');
  if (btn) { btn.disabled = true; btn.title = '登出中…'; }
  // 先通知服务端（尽力而为）：即便它失败/超时，本地也必须登出 ——
  // 「点登出没反应」比「服务端还留着一条会话」糟糕得多。
  try { await API.post('/api/auth/logout', {}); } catch (_) { /* 忽略 */ }
  PAuth.setAuthToken('');
  PAuth.resetSessionId();
  PAuth.sessionDead = true;
  // **为什么直接 reload 而不是手工清理**：主界面内存里散着上一个用户的
  // lastBars / lastRecord / 各面板 innerHTML / 叠加层 / 追问线程 / 当前订阅。
  // 手工清理正是本仓库反复踩坑的地方（clearOverlays 漏派生图例、
  // 置 lastRecord=null 面板内容还在…）。reload 让「干净」由构造保证。
  // 代价是一次静态资源重载 —— 可接受，且换用户时本来就该重来。
  location.reload();
}

/** 会话失效（令牌被服务端拒绝 / 本地过期 / 被登出）的统一处置。
 *  **刻意不 reload**：boot 期间并发的一批请求会一起 401，reload 会变成
 *  「重载 → 首屏请求又 401 → 再重载」的循环。切登录页是幂等且有终点的。 */
function onSessionLost(reason) {
  showLoginScreen(reason === 'missing' ? 'missing' : 'expired', 'error');
  showToast(
    reason === 'missing' ? '登录状态丢失，请重新登录' : '登录已失效，请重新登录',
    'warning',
    { force: true }
  );
  const el = _loginEls();
  if (el.pass) el.pass.value = '';
}

/** 启动闸门。**返回 true 才允许继续 boot**。
 *  任何异常都按「拒绝进入」处理（fail closed），且**绝不抛**。 */
async function initLoginGate() {
  const el = _loginEls();
  // 1) handler 先绑：闸门自己失败也不该让登录框失去响应
  if (el.form) el.form.addEventListener('submit', handleLoginSubmit);
  if (el.pass) {
    el.pass.addEventListener('input', () => { if (el.error && !el.error.hidden) setLoginError(''); });
  }
  PAuth.setUnauthorizedHandler(onSessionLost);

  try {
    // 2) 同步判定 —— 不 await，所以匿名用户**看不到任何一帧空主界面**。
    //    没有令牌就到此为止：**一个请求都不发**（含 /api/auth/me —— 没有令牌时
    //    问它只会换一个 401 回来，纯粹浪费一次往返）。
    if (PAuth.decideBootGate(PAuth.readAuthToken()) === 'login') {
      showLoginScreen('missing');
      const u = _loginEls().user;
      if (u) setTimeout(() => u.focus(), 0);
      return false;
    }
    // 3) 有令牌 → 向服务端问一次身份，**由它说了算**（前端不自己判过期，
    //    见 web/api/routes_auth.py 前端契约第 1 条）。
    //    **只有 401 才拦**：404 / 500 / 网络抖动一律放行。理由是这一步是
    //    「锦上添花的身份回显」，真正的鉴权由业务端点自己把关（它们 401 会
    //    走 onSessionLost）；而如果因为这个可选端点一抖就把人锁在登录页，
    //    代价远大于收益。
    const headers = await sessionHeadersAsync();
    const r = await fetch('/api/auth/me', { headers, cache: 'no-cache' });
    if (r.status === 401) {
      PAuth.clearAuthToken();
      showLoginScreen('expired', 'error');
      return false;
    }
    if (r.ok) {
      let me = null;
      try { me = await r.json(); } catch (_) { /* 非 JSON 就只用令牌里的 sub */ }
      setAuthBadge(_displayNameOf(me) || PAuth.currentUserId());
    }
    document.body.dataset.auth = 'app';
    return true;
  } catch (_) {
    // fetch 本身炸了（网络/被拦截/扩展干扰）：不能假定有身份。
    PAuth.clearAuthToken();
    showLoginScreen('network', 'error');
    setLoginError('无法连接服务端，请检查后端是否已启动');
    return false;
  }
}

// ── Init ───────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  // 🔑 登录闸门：必须在任何 createChart / bindEvents / 数据加载之前。
  // 未登录时**直接 return**，主界面一行代码都不执行 ⇒ 一个业务请求都不会发。
  if (!(await initLoginGate())) return;

  initSidebarResizer();  // 恢复侧边栏宽度（Phase A Task 1），需在 createChart 前完成以避免布局抖动
  const { chart: c, candleSeries: cs, emaSeries: es, emaSeriesMap: em } = createChart($('#chart-main'));
  chart = c; candleSeries = cs; emaSeries = es; emaSeriesMap = em;
  // 移除 LightweightCharts 设置的固定 width，让 flex:1 布局接管容器宽度
  // 这样 sidebar 折叠/展开时 chart-main 能正确自适应
  const chartMainEl = $('#chart-main');
  if (chartMainEl) {
    chartMainEl.style.removeProperty('width');
    chartMainEl.style.width = '100%';
  }

  // 🔑 关键：先注册 UI 事件 handler，再做数据加载
  // 原因：loadBars() 在异常时会重新抛出（throw e），如果 bindEvents 在 await 之后调用，
  // 任何数据加载失败都会导致 bindEvents 永远不执行 — 所有按钮失去响应。
  // 将 bindEvents 提前到 await 之前，确保 UI 在任何数据加载失败时也可交互。
  bindEvents();

  await loadSettings();      // 先加载 settings，回填交易所/品种/周期
  await loadExchanges();     // 再加载交易所下拉（并选中当前值）
  await loadSymbols();       // 根据当前交易所加载品种列表
  await loadTimeframes();    // 加载周期下拉（并选中当前值）
  await loadOrderOpportunityTypes();  // 从后端拉下单机会订单类型（避免前后端枚举漂移）
  await loadBars();          // 最后拉 K 线
  loadHistoryList();         // 拉当前 (exchange, symbol, timeframe) 的历史分析记录
  setDataMode('live');              // 把模式状态同步到 body / 按钮 / 只读态
  refreshIncrementalButtonState();  // 判定有无可复用上下文 → 「分析」按钮文案/提示
  // 追问历史回填：刷新后前端内存里啥都没有，但服务端仍能用「同一 session_id
  // + 从记录自推导的 record 段」命中同一个线程。enableChat() 内部会触发回填。
  // 必须放在 setDataMode('live') 之后 —— 只读态判定依赖它。
  if (typeof enableChat === 'function') enableChat();
  // 经验库范围恒等于当前订阅（交易对 + 周期），切品种/周期后必须同步刷新，
  // 否则面板会停在上一个标的的结果上。
  if (typeof loadExperienceLibrary === 'function') {
    loadExperienceLibrary({ all: typeof _expShowAll !== 'undefined' && _expShowAll });
  }

  // 注册指标库上下文（主图 + 副图容器），恢复已保存的指标
  if (window._indicatorsAPI) {
    const oscWrap = document.getElementById('chart-osc-wrap');
    window._indicatorsAPI.registerContext(chart, candleSeries, oscWrap);
    // 首次数据已加载，触发指标渲染
    window._indicatorsAPI.onBarsUpdated(lastBars || []);
  }
  resizeChart();

  // 初始化时检查是否需要启动倒计时显示
  const cbWait = $('#cb-wait-close');
  const cbKeep = $('#cb-keep-analysis');
  if ((cbWait && cbWait.checked) || (cbKeep && cbKeep.checked)) {
    startWaitingCountdownDisplay();
  }

  // ── 健康状态指示 ──────────────────────────────────────────────────
  (function initHealthBanner() {
    const banner = $('#health-banner');
    if (!banner) return;
    let closed = false;

    function updateHealth() {
      if (closed) return;
      API.get('/api/health').then(data => {
        const status = data?.status || 'ok';
        if (status === 'ok') {
          banner.classList.add('hidden');
          banner.className = 'health-banner hidden';
        } else {
          banner.className = 'health-banner ' + status;
          const messages = {
            starting: '系统启动中，请稍候…',
            degraded: '部分服务异常，功能可能受限',
            error: '服务异常，请检查后端状态',
          };
          banner.innerHTML = (messages[status] || '状态未知') +
            ' <button class="close-btn" onclick="this.parentElement.classList.add(\'hidden\')">&times;</button>';
        }
      }).catch(() => {}); // 静默忽略网络错误
    }

    updateHealth();
    setInterval(updateHealth, 60000); // 每 60 秒轮询
  })();
});

window.addEventListener('resize', resizeChart);

function resizeChart() {
  const mainEl = $('#chart-main');
  const paneEl = $('#chart-pane');
  if (!mainEl || !paneEl) return;
  // 主图宽度 = chart-pane 宽度；主图高度 = chart-main 实际高度（由 flex 布局计算）
  const paneWidth = paneEl.clientWidth;
  if (chart) {
    chart.applyOptions({ width: paneWidth, height: mainEl.clientHeight });
  }
  // 副图容器宽度跟随画布
  const oscWrap = document.getElementById('chart-osc-wrap');
  if (oscWrap) {
    oscWrap.style.width = paneWidth + 'px';
    // 通知每个副图调整宽度（高度由 _layoutOscillatorPanes 单独管理）
    if (window._indicatorsState) {
      for (const inst of window._indicatorsState.activeIndicators) {
        if (inst._subChart) {
          try { inst._subChart.applyOptions({ width: paneWidth }); } catch (_) {}
        }
      }
    }
  }
  // 触发副图重新布局（主图高度变化后副图也需调整）
  if (window._indicatorsAPI && window._indicatorsAPI.relayoutPanes) {
    try { window._indicatorsAPI.relayoutPanes(); } catch (_) {}
  }
}

// ── Data loading ───────────────────────────────────────────────────────
// ── 图表数据唯一入口 ────────────────────────────────────────────────────────
// setSeries + setSeqMarkers + 指标重算 + 时间锚点更新，必须**成套**执行：
// 只调 setBars 会让 EMA/MACD 继续持有上一个品种的数据（实测切到 BTCUSDT 后
// EMA 仍是 NVDA 的 210~234，而蜡烛是 48000~64000），主图自动缩放把两个数量级
// 一起纳入，价格轴被拉到 -8000~66000，K 线被压成顶部一条、指标线贴地。
// demo 上一轮就是这么坏的。这里收口成唯一入口，新增数据源只需调它。
function applyBarsToChart(bars, opts) {
  const options = opts || {};
  const list = Array.isArray(bars) ? bars : [];
  setBars(candleSeries, list);
  if (options.seqMarkers !== false) setSeqMarkers(candleSeries, list);
  if (list.length) {
    lastBars = list;
    const sorted = [...list].sort((a, b) => a.ts_open - b.ts_open);
    window.__PA_LAST_BAR_TIME__ = sorted[sorted.length - 1].ts_open / 1000;
  }
  if (window._indicatorsAPI && window._indicatorsAPI.onBarsUpdated) {
    window._indicatorsAPI.onBarsUpdated(list);
  }
  return list;
}

async function loadBars() {
  try {
    _demoBarsStale = false;
    const barCount = parseInt($('#ds-bar-count')?.value) || 100;
    const data = await API.get(`/api/bars?count=${barCount}`);
    applyBarsToChart(data.bars || []);
    // 休市检测：bars[0].closed == true 表示无 forming bar（市场已收盘）
    // 清空 sseNextCloseTs 避免取模算法返回错误未来值，触发「休市中」显示
    if (lastBars.length && lastBars[0] && lastBars[0].closed === true) {
      sseNextCloseTs = 0;
    }
    // 通知指标库重新计算并渲染（含 EMA/SMA/BOLL/RSI/MACD/KDJ 等）
    fitView(chart, 20, lastBars.length);
    // 主图 fitView 后同步副图时间轴
    if (window._indicatorsAPI && window._indicatorsAPI.syncSubCharts) {
      window._indicatorsAPI.syncSubCharts();
    }
    liveRefreshLastTs = Date.now();
    updateLiveRefreshStatus();
  } catch (e) {
    console.error('loadBars:', e);
    // 向上抛出，由 applySubscribe 捕获并提示（switch-performance-refactor spec）
    throw e;
  }
}

// 仅刷新 K 线数据（用于实时刷新，不重置 overlay）
async function refreshBarsOnly() {
  try {
    const data = await API.get('/api/bars?count=100');
    lastBars = data.bars || [];
    // 分析期间暂停图表 K 线渲染（lastBars 仍然更新，保证持续分析的收盘判定
    // 用的是最新数据；分析结束后的 loadBars() 会把图表一次性刷新到位）
    if (chartUpdatePaused) {
      liveRefreshLastTs = Date.now();
      updateLiveRefreshStatus();
      return;
    }
    applyBarsToChart(lastBars);
    if (lastBars.length) {
      const sorted = [...lastBars].sort((a, b) => a.ts_open - b.ts_open);
      window.__PA_LAST_BAR_TIME__ = sorted[sorted.length - 1].ts_open / 1000;
    }
    // 通知指标库重新计算并渲染
    liveRefreshLastTs = Date.now();
    updateLiveRefreshStatus();
  } catch (e) {
    console.error('refreshBarsOnly:', e);
  }
}

// ── LLM 来源开关（provider.use_custom）──────────────────────────────────
// 后端语义：use_custom=false 时**整段忽略**用户对 provider 的覆盖，模型来自出厂
// 配置；true 时用户在设置页填的 model / base_url / api_key 才生效。
// 因此界面必须做到两件事，二选一开关本身就是二者的总闸。
const LLM_SOURCE_SYSTEM = '#s-llm-source-system';
const LLM_SOURCE_CUSTOM = '#s-llm-source-custom';
const LLM_CUSTOM_FIELDS = '#s-llm-custom-fields';

// 切换开关并同步 LLM 配置项的可见性。
// **只切 hidden，绝不清空输入框的值**：用户填到一半的 model/api_key 必须在
// 切到「系统默认」再切回来时仍在；掩码值（abcd****wxyz）也原样保留，
// 提交路径的掩码保护（后端 _should_keep_existing）继续生效。
function setLlmSourceMode(useCustom) {
  const custom = useCustom === true;
  const sysRadio = $(LLM_SOURCE_SYSTEM);
  const customRadio = $(LLM_SOURCE_CUSTOM);
  if (sysRadio) sysRadio.checked = !custom;
  if (customRadio) customRadio.checked = custom;
  // 隐藏而非 disabled：这些字段此刻完全不参与计算，不该出现在界面上
  $(LLM_CUSTOM_FIELDS)?.classList.toggle('hidden', !custom);
  const hint = $('#s-llm-source-hint');
  if (hint) {
    hint.textContent = custom
      ? '将使用你在下面填写的模型配置，忽略系统出厂默认值。'
      : '模型与接口全部来自系统出厂配置，下面的配置项不会生效。';
  }
  return custom;
}

// 读开关当前状态（保存时用）。缺省 false：后端默认也是 false。
function isLlmCustomSelected() {
  return $(LLM_SOURCE_CUSTOM)?.checked === true;
}

/* ── 会话游标 → <select> 回显（唯一真源 = 服务端游标） ─────────────────────
 *
 * 一个游标轴在页面上有**三处**投影，必须始终指向同一个值：
 *   1. `dataset.current` —— **权威载体**。`loadExchanges()` / `loadTimeframes()`
 *      会整块重写 `<select>` 的 innerHTML，`.value` 随之清零；只有 dataset
 *      能活过这次重建，并被它们读出来标 `<option selected>`。
 *   2. `.value` —— 用户实际看见/被上层读到的值。
 *   3. `currentSettings.general.last_*` —— 服务端快照，本函数的**数据来源**。
 * 三者不是三个真源：服务端游标是唯一的，dataset 是它的载体，`.value` 是载体
 * 在「选项已就位」时的投影。
 *
 * ⚠ 为什么这里**不能**直接 `sel.value = v`
 *
 * init 顺序是 loadSettings() → loadExchanges()（app.js:522-523），此刻
 * `<select>` 里**一个 `<option>` 都还没有**（index.html 只给了空壳标签）。
 * 按 HTML 规范，`select.value = <不存在的值>` 会把选中集清空、`.value`
 * 静默变成 `''`（实测 selectedIndex=-1）。那比不写更糟：下拉框不再是
 * 「未回显」，而是「回显成空」，紧随其后的 loadSymbols() 会拿空交易所去
 * 要品种列表。
 *
 * 故策略是**挂起**：dataset 先记下，选项就位后由 loadExchanges/
 * loadTimeframes 兑现成 `.value`；选项已在（applySubscribe 后再调
 * loadSettings 的那条路径）则当场兑现，并对不上时报错而不是静默改选。
 */
function setCursorSelect(sel, value) {
  // undefined/null = 服务端没给这一轴，保持原样（别用空值覆盖已知游标）
  if (!sel || value === undefined || value === null) return;
  const v = String(value);
  // ① 权威载体：选项在不在都写。它同时是「挂起」这个机制本身。
  sel.dataset.current = v;

  // ② 选项尚未就位 → 到此为止。loadExchanges/loadTimeframes 会读 dataset.current
  //    选中对应 <option>，这正是「先 settings 后 exchanges」顺序的用途。
  const opts = sel.options || [];
  if (!opts.length) return;

  // ③ 选项已就位：兑现 .value，让 dataset 与可见值不漂移
  //    （applySubscribe 成功后重调 loadSettings 走的就是这条路径）。
  const hit = opts.find(o => String(o.value) === v);
  if (!hit) {
    // 对不上时**绝不**静默落到「自动（探测）」或首个 option —— 那会让用户
    // 以为在看 A 标的，实际按 B 标的取数，且全程无报错。
    console.error(`[cursor] #${sel.id}: 服务端游标 ${JSON.stringify(v)} 不在选项列表中，`
      + `保持 ${JSON.stringify(sel.value)}`);
    if (typeof showSwitchError === 'function') {
      showSwitchError(`服务端会话游标「${v}」不在${sel.id === 'ds-exchange' ? '交易所' : '周期'}列表中，请手动选择`, 'symbol');
    }
    return;
  }
  if (sel.value !== v) sel.value = v;
}

/** 读一个游标轴：DOM 有合法选中就用它，否则回落到服务端游标。
 *  UI 回显与出站请求共用这一个解析口 —— 规则只写一遍，两者不可能各写各的。 */
function cursorValue(sel, fallback) {
  const dom = sel && sel.value ? String(sel.value) : '';
  if (dom) return dom;
  return (fallback === undefined || fallback === null) ? '' : String(fallback);
}

async function loadSettings() {
  try {
    const s = await API.get('/api/settings');
    currentSettings = s;
    // 先填值再切可见性：隐藏状态下值照样留在 DOM 里，切回「自带模型」时即可见。
    $('#s-base-url').value = s.provider?.base_url || '';
    $('#s-model').value = s.provider?.model || '';
    $('#s-api-key').value = s.provider?.api_key || '';
    $('#s-reasoning-effort').value = s.provider?.reasoning_effort || 'high';
    $('#s-thinking').checked = s.provider?.thinking !== false;
    $('#s-max-output-tokens').value = s.provider?.max_output_tokens || 0;
    // use_custom 缺省 false（跟随系统）—— 与后端 AIProviderSettings 默认值一致。
    setLlmSourceMode(s.provider?.use_custom === true);
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
    // 回填工具栏的交易所/品种/周期。
    // #ds-symbol 是 <input type="hidden">，.value 恒合法，直接写。
    // #ds-exchange/#ds-timeframe 是**空壳 <select>**（选项由 loadExchanges/
    // loadTimeframes 注入），此处只能经 setCursorSelect 写 dataset 载体 ——
    // 直接 .value = 'NASDAQ' 会因选项尚未就位而静默变 ''（详见该函数注释）。
    $('#ds-symbol').value = s.general?.last_symbol || 'BTCUSDT';
    setCursorSelect($('#ds-exchange'), s.general?.last_tradingview_exchange);
    setCursorSelect($('#ds-timeframe'), s.general?.last_timeframe);
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
    // TradingView 凭证
    const tv = s.tradingview || {};
    const tvSessionIdEl = $('#s-tv-session-id');
    const tvUserEl = $('#s-tv-username');
    const tvPassEl = $('#s-tv-password');
    if (tvSessionIdEl) tvSessionIdEl.value = tv.session_id || '';
    if (tvUserEl) tvUserEl.value = tv.username || '';
    if (tvPassEl) tvPassEl.value = tv.password || '';

    // 后端把已配置的凭据以 abcd****wxyz 形式返回（GET /api/settings 脱敏）。
    // 提示用户「已配置，留空不改」——保存时后端会识别占位值并保留原值，
    // 用户无需重新输入即可保存其它设置。
    markMaskedSecretFields();

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

// 交易所 id → 可读名（如 GATEIO → Gate.io、SSE → 上交所、NASDAQ → 纳斯达克）。
// 真源是后端 `web/api/routes_data.py::list_tv_exchanges` 的 label_map，由
// `/api/tv/exchanges` 下发；**前端不另抄一份** —— 抄一份必然与后端漂移，
// 而漂移的后果是「历史列表里显示了一个已经改掉的旧名」。
let exchangeLabels = {};

async function loadExchanges() {
  try {
    const list = await API.get('/api/tv/exchanges');
    // 历史列表跨品种浏览时要标注交易所（NVDA 在 NASDAQ 与 GATEIO 下同名同周期，
    // 只看 `NVDA·5m` 分辨不出来）。同一个响应顺带缓存成可读名映射。
    const labels = {};
    (list || []).forEach(d => { if (d && d.id !== undefined) labels[d.id] = d.label || d.id; });
    exchangeLabels = labels;
    const sel = $('#ds-exchange');
    const cur = sel.dataset.current || '';
    sel.innerHTML = list.map(d => `<option value="${d.id}"${d.id === cur ? ' selected' : ''}>${d.label}</option>`).join('');
  } catch (e) {
    // 保留旧缓存：加载失败只降级成裸代号，不该把已知名字也一起丢掉
    console.error('loadExchanges:', e);
  }
}

// 交易所的展示名。**必须有名字**：这次要修的病就是「看不出这条属于哪个交易所」，
// 映射没加载 / 遇到后端尚未收录的 id 时回落到裸代号，绝不返回空串
//（空串会让那条记录**比原来更难分辨**，等于把 bug 换了个形态）。
function exchangeDisplayName(exchange) {
  const raw = String(exchange == null ? '' : exchange).trim();
  if (!raw) return '';
  return exchangeLabels[raw] || raw;
}

async function loadSymbols() {
  const exchange = $('#ds-exchange').value || '';
  // 缓存命中：10 分钟内已请求过该交易所，直接用缓存渲染
  if (symbolListCache.has(exchange)
      && (Date.now() - (symbolListCacheTs.get(exchange) || 0)) < SYMBOL_CACHE_TTL) {
    const cached = symbolListCache.get(exchange) || [];
    symbolList = cached;
    // 重新渲染下拉（用户可能切回了之前的交易所，需要恢复搜索框内容）
    const curSymbol = $('#ds-symbol').value || (currentSettings?.general?.last_symbol || 'BTCUSDT');
    const searchInput = $('#ds-symbol-search');
    if (searchInput) searchInput.value = curSymbol;
    // 若搜索框已展开下拉，立即用缓存刷新结果
    const dropdown = $('#symbol-search-dropdown');
    if (dropdown && !dropdown.hasAttribute('hidden')) {
      filterSymbolList(searchInput ? searchInput.value.trim() : '');
    }
    return;
  }
  // 去重：同一交易所并发请求时只发一次，后续调用直接 return
  if (_inflightExchange === exchange) return;
  _inflightExchange = exchange;
  try {
    const data = await API.get(`/api/tv/symbols?exchange=${encodeURIComponent(exchange)}`);
    const syms = data.symbols || [];
    // 写入缓存
    symbolListCache.set(exchange, syms);
    symbolListCacheTs.set(exchange, Date.now());
    symbolList = syms;
    const curSymbol = $('#ds-symbol').value || (currentSettings?.general?.last_symbol || 'BTCUSDT');
    const searchInput = $('#ds-symbol-search');
    if (searchInput) {
      searchInput.value = curSymbol;
    }
  } catch (e) {
    console.error('loadSymbols:', e);
  } finally {
    _inflightExchange = null;
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

// 拉取后端单一真源的下单机会订单类型（pa_agent.ai.order_opportunity.ORDER_OPPORTUNITY_TYPES）。
// 失败时保留内置默认值，避免下单提醒整体失效。
async function loadOrderOpportunityTypes() {
  try {
    const list = await API.get('/api/order-opportunity-types');
    if (Array.isArray(list) && list.length) {
      ORDER_OPPORTUNITY_TYPES = list;
    }
  } catch (e) {
    console.warn('loadOrderOpportunityTypes: 使用内置默认值', e);
  }
}

// 标记「已配置但已脱敏」的凭据输入框：加 CSS 类 + placeholder 提示。
// 后端 GET /api/settings 只回传 abcd****wxyz，占位值在保存时会被后端忽略，
// 因此用户保持占位值即可保存，不会清空真实凭据。
const MASKED_FIELD_IDS = [
  '#s-api-key', '#s-feishu-webhook', '#s-feishu-secret', '#s-feishu-app-secret',
  '#s-pushplus-token', '#s-tushare-token', '#s-tv-session-id', '#s-tv-password',
];

function markMaskedSecretFields() {
  MASKED_FIELD_IDS.forEach((sel) => {
    const el = $(sel);
    if (!el) return;
    const masked = /\*{4}/.test(el.value || '');
    el.classList.toggle('is-masked-secret', masked);
    if (masked && !el.dataset.maskedHint) {
      el.dataset.maskedHint = '1';
      el.dataset.origPlaceholder = el.placeholder || '';
      el.placeholder = '已配置（留空或保持不变即可）';
    }
  });
}

// ── Events ─────────────────────────────────────────────────────────────
let currentAnalysisStream = null;

function bindEvents() {
  // 记住原始 placeholder，退出只读态时还原
  const ci0 = $('#chat-input');
  if (ci0 && !ci0.dataset.ph) ci0.dataset.ph = ci0.placeholder || '';
  // 登出：handler 放在这里（而不是登录闸门里）—— 顶栏属于主界面，
  // 闸门放行的路径上它必然会被绑定；反过来放进闸门则会出现
  // 「已登出但按钮还没绑定」的中间态。
  $('#btn-logout')?.addEventListener('click', doLogout);
  $('#btn-refresh').addEventListener('click', loadBars);
  // 恢复图表按钮：自适应缩放显示所有数据
  const btnFitView = $('#btn-fit-view');
  if (btnFitView) {
    btnFitView.addEventListener('click', () => {
      if (typeof chart !== 'undefined' && chart && chart.timeScale) {
        chart.timeScale().fitContent();
      }
    });
  }
  // 合并分析按钮：根据当前状态决定是「开始分析」还是「取消分析」
  // idle / waiting → 启动分析（waiting 表示已勾选等待收盘，点击即启动倒计时+分析）
  // analyzing → 取消分析
  const btnAnalyzeToggle = $('#btn-analyze-toggle');
  if (btnAnalyzeToggle) {
    btnAnalyzeToggle.addEventListener('click', () => {
      const state = btnAnalyzeToggle.dataset.state || 'idle';
      if (state === 'analyzing') {
        if (currentAnalysisStream) {
          currentAnalysisStream.abort();
        }
        stopWaitCloseCountdown();
      } else {
        // idle 或 waiting → 启动分析
        startAnalysis();
      }
    });
  }
  // 「强制完整」开关：切换后立刻让「分析」按钮反映它将走哪条路
  $('#cb-force-full')?.addEventListener('change', updateAnalyzeButtonHint);
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
  // 「全部品种」开关：切到跨品种浏览（历史是 L2 用户级共享数据，多标签页通用）
  const chkHistAll = $('#chk-history-all-symbols');
  if (chkHistAll) {
    chkHistAll.addEventListener('click', (e) => {
      e.stopPropagation();
      loadHistoryList();
    });
  }
  // 返回实时按钮：清除回看状态，隐藏 badge，切回 stream tab
  const btnBack = $('#btn-live');
  if (btnBack) {
    btnBack.addEventListener('click', async () => {
      const dataModeWas = currentDataMode();
      hideReplayBadge();
      // 重置所有分析产出面板（预测 / 决策树 / 决策 / 流式 / token 用量）。
      // 只置 lastRecord=null 是不够的：面板的 innerHTML 里仍留着上一条记录
      // 的渲染结果 —— 实机报告的「切回实时还有页面数据没清空」就是这个。
      resetAnalysisPanels();
      $$('.sidebar-tabs .tab').forEach(b => b.classList.remove('active'));
      document.querySelector('.sidebar-tabs .tab[data-tab="stream"]')?.classList.add('active');
      $$('.tab-panel').forEach(p => p.classList.remove('active'));
      $('#tab-stream').classList.add('active');
      setDataMode('live');

      // Demo 会覆盖主图数据却不动订阅（品种/周期可能和演示内容对不上），
      // 因此从 Demo 返回必须**无条件重载真实 K 线**，不能只清叠加层。
      if (dataModeWas === 'demo' || isDemoDataActive()) {
        _liveSubBeforeReplay = null;
        if (window._indicatorsAPI) window._indicatorsAPI.clearAllData();
        clearOverlays(candleSeries);
        clearExperienceLegend();
        await loadBars();
        showToast('已返回实时行情', 'success');
        return;
      }

      // 回看期间主图被切到了历史记录的品种，这里必须恢复实时订阅，
      // 否则「返回实时」只是切了 tab，K 线还停在历史品种上。
      const live = _liveSubBeforeReplay;
      _liveSubBeforeReplay = null;
      if (!live || !live.symbol) {
        await loadBars();   // 订阅没变也要把演示/残留数据刷回真实 K 线
        return;
      }
      const nowSym = $('#ds-symbol')?.value || '';
      const nowTf = $('#ds-timeframe')?.value || '';
      if (nowSym === live.symbol && nowTf === live.timeframe) {
        clearOverlays(candleSeries);
        await loadBars();
        return;
      }
      try {
        if (window._indicatorsAPI) window._indicatorsAPI.clearAllData();
        const wasLive = $('#cb-live-refresh')?.checked;
        if (wasLive) stopSSEBarsStream();
        await API.post('/api/subscribe', {
          kind: 'tradingview',
          symbol: live.symbol,
          timeframe: live.timeframe,
          exchange: live.exchange,
        }, { timeout: 15000 });
        await loadBars();
        clearOverlays(candleSeries);
        // ⚠ 工具栏游标必须**先于** startSSEBarsStream 写好：轮询流的
        // startNextClosePolling 会同步发起一次 next-close 请求，读的是
        // #ds-symbol/#ds-exchange/#ds-timeframe；顺序反了会按旧游标取数。
        const hid = $('#ds-symbol'); if (hid) hid.value = live.symbol;
        const shown = $('#ds-symbol-search'); if (shown) shown.value = live.symbol;
        const tf = $('#ds-timeframe'); if (tf) tf.value = live.timeframe;
        // 走 setCursorSelect 而不是裸写 .value：历史记录的交易所若不在当前
        // 选项列表里，裸写会让 select.value **静默变 ''**（比不回显更糟），
        // 且 dataset.current 不会被更新，下一次重渲染又跳回旧值。
        if (live.exchange) setCursorSelect($('#ds-exchange'), live.exchange);
        if (wasLive) startSSEBarsStream();
        showToast(`已返回实时：${live.symbol} ${live.timeframe}`, 'success');
      } catch (err) {
        console.error('backToLive:', err);
        showToast('返回实时失败，请手动切换品种', 'error');
      }
    });
  }
  // Demo 按钮：加载 Demo 数据体验 UI
  const btnDemo = $('#btn-demo');
  if (btnDemo) {
    btnDemo.addEventListener('click', async () => {
      try {
        btnDemo.disabled = true;
        btnDemo.textContent = '加载中...';
        const data = await API.get('/api/demo/sample');
        // 演示数据进入 demo 模式：状态灯点亮 + 状态条说明，避免被当成真实行情
        setDataMode('demo', data.sample?.symbol ? `${data.sample.symbol} · 模拟行情` : '模拟行情');
        _demoBarsStale = true;
        // 设置 lastRecord 并渲染各 tab
        lastRecord = data;
        renderDecision(data);
        renderFuturePanel(data);
        renderDecisionTree(data);
        renderRaw(data);
        renderDebug(data);
        renderTokenUsage(data.usage_total);
        updateTokenProgress(data.usage_total);
        renderTreeViz(data);
        // 更新 K 线图表
        //
        // 契约：setBars() 接收的是**原始 bar**（带 ts_open/closed），由它自己
        // 升序排序并换算成 LightweightCharts 的秒级 time。此前这里先手工映射成
        // {time, open, ...}，于是 setBars 拿到 a.ts_open === undefined：
        //   - 排序比较 undefined → 退化成原序
        //   - time: b.ts_open / 1000 → NaN
        // LightweightCharts 直接抛 "Value is null" / "right should be >= left"，
        // 结果是图表仍是上一个品种的 K 线，而 Entry/SL/TP 横线画在另一个价格
        // 区间上（实测 NVDA 206-240 的图上出现 60413-68509 的决策价），
        // 「决策 ↔ K 线」彻底脱钩。
        if (data.kline_data && data.kline_data.length) {
          // applyBarsToChart 内部成套完成：蜡烛 + 序号标记 + 指标重算 + 时间锚点。
          // （曾漏掉指标重算，导致 EMA/MACD 继续持有上一个品种的数据。）
          applyBarsToChart(data.kline_data);
          chart.timeScale().fitContent();
          // 工具栏同步 demo 的品种/周期，避免「图上是 BTCUSDT、工具栏写 NVDA」。
          // #ds-symbol 是隐藏域（真实订阅状态），#ds-symbol-search 是可见展示框，
          // 两者都要更新，否则用户看到的品种与图上的数据对不上。
          const demoSym = data.symbol, demoTf = data.timeframe;
          if (demoSym) {
            const hidden = $('#ds-symbol'); if (hidden) hidden.value = demoSym;
            const shown = $('#ds-symbol-search'); if (shown) shown.value = demoSym;
          }
          if (demoTf) { const el = $('#ds-timeframe'); if (el) el.value = demoTf; }
        }
        // 更新决策叠加层
        const overlay = data.decision_overlay || data.stage2_decision || {};
        setDecisionOverlays(candleSeries, overlay);
        setDirectionMarker(candleSeries, overlay);
        // Demo 同样代表一次完整分析，走完后必须解锁追问 ——
        // 此前只在真实分析的 done 事件里调 enableChat()，导致 demo 下
        // 追问输入框始终禁用，等于演示时这条主交互根本用不了。
        enableChat();
        renderChatContext();
        // 切换到决策 tab
        $$('.sidebar-tabs .tab').forEach(b => b.classList.remove('active'));
        document.querySelector('.sidebar-tabs .tab[data-tab="decision"]')?.classList.add('active');
        $$('.tab-panel').forEach(p => p.classList.remove('active'));
        $('#tab-decision').classList.add('active');
      } catch (err) {
        console.error('Demo 加载失败:', err);
        // TRAE 内置 webview 中 alert() 会触发崩溃（AGENTS.md UI 风格）
        showToast('Demo 数据加载失败: ' + err.message, 'error');
      } finally {
        btnDemo.disabled = false;
        btnDemo.textContent = 'Demo';
      }
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
  // 品种搜索输入事件
  const symSearchInput = $('#ds-symbol-search');
  if (symSearchInput) {
    symSearchInput.addEventListener('input', handleSymbolSearch);
    // 事件委托：结果项不再用内联 onclick（字符串插值有注入风险，
    // 且品种名/代码含引号会直接破坏 HTML）。
    const symResults = document.querySelector('.symbol-search-results');
    if (symResults) {
      symResults.addEventListener('click', (ev) => {
        const item = ev.target.closest('.symbol-search-item');
        if (!item) return;
        const sym = item.dataset.symbol;
        if (sym) selectSymbol(sym);
      });
      symResults.addEventListener('mousemove', (ev) => {
        const item = ev.target.closest('.symbol-search-item');
        if (!item) return;
        const idx = Number(item.dataset.index);
        if (!Number.isNaN(idx) && idx !== symbolSearchSelectedIndex) {
          symbolSearchSelectedIndex = idx;
          symResults.querySelectorAll('.symbol-search-item').forEach(el => {
            el.classList.toggle('selected', Number(el.dataset.index) === idx);
          });
        }
      });
    }
    symSearchInput.addEventListener('focus', showSymbolDropdown);
    symSearchInput.addEventListener('keydown', handleSymbolSearchKeydown);
  }
  // 清除搜索按钮
  const symClearBtn = $('#btn-symbol-clear');
  if (symClearBtn) {
    symClearBtn.addEventListener('click', clearSymbolSearch);
  }
  // 点击外部关闭搜索下拉
  document.addEventListener('click', (e) => {
    const dropdown = $('#symbol-search-dropdown');
    const searchContainer = $('.symbol-search-container');
    if (dropdown && !dropdown.hasAttribute('hidden') && !searchContainer.contains(e.target)) {
      dropdown.setAttribute('hidden', '');
    }
  });
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

  // 侧边栏折叠/展开（工具栏按钮 + 折叠后浮出按钮）
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

  // 模型来源二选一：切到「系统默认」即隐藏整块自定义配置，切回时原值仍在。
  // 读的是**自定义那个 radio** 的状态，不能读事件目标自己的 checked ——
  // 选中「系统默认」时事件目标恰好是 checked=true，直接用它会把语义整个反过来。
  [LLM_SOURCE_SYSTEM, LLM_SOURCE_CUSTOM].forEach((sel) => {
    $(sel)?.addEventListener('change', () => setLlmSourceMode(isLlmCustomSelected()));
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

// ── 面板内子 tab（可视化→决策树、调试→原始）────────────────────────────
  // 两组被合并的面板是同级兄弟、共享同一槽位，切换即在两者间转移 .active。
  const SUBTAB_GROUPS = [['stream', 'raw', 'debug'], ['tree', 'tree-viz']];

  // 组是「所属顶层 tab」，必须按成员反查；
  // 直接 SUBTAB_GROUPS[target] 在 target='tree-viz' 时查不到，
  // 导致旧的 #tab-tree 没被移除，两个面板同时占位。
  function groupOf(target) {
    return SUBTAB_GROUPS.find(g => g.includes(target)) || null;
  }

  function syncSubtabBar(activeKey) {
    $$('.subtabs').forEach(bar => {
      $$('.subtab', bar).forEach(btn => {
        btn.classList.toggle('active', btn.dataset.subtab === activeKey);
      });
    });
  }

  $$('.subtab').forEach(btn => {
    btn.addEventListener('click', () => {
      const target = btn.dataset.target;
      const panel = document.querySelector(`#tab-${target}`);
      if (!panel) return;
      const group = groupOf(target);
      if (group) {
        group.forEach(k => document.querySelector(`#tab-${k}`)?.classList.remove('active'));
      } else {
        // 单视图面板（如「追问」）没有同组兄弟。正常情况下用户点不到隐藏
        // 面板里的子 tab，但仍要防御：否则会给侧边栏留下两个 .active
        // 面板（先前曾出现 tab-tree + tab-raw 同时可见）。
        $$('.tab-panel.active').forEach(p => p.classList.remove('active'));
      }
      panel.classList.add('active');
      btn.parentElement.querySelectorAll('.subtab').forEach(b => {
        b.classList.toggle('active', b === btn);
      });
      // 切到流程图时需要按需重渲染（内部状态可能陈旧）
      if (target === 'tree-viz' && lastRecord && typeof renderTreeViz === 'function') {
        renderTreeViz(lastRecord);
      } else if (target === 'debug' && lastRecord && typeof renderDebug === 'function') {
        renderDebug(lastRecord);
      } else if (target === 'tree' && lastRecord && typeof renderDecisionTree === 'function') {
        renderDecisionTree(lastRecord);
      } else if (target === 'stream' && lastRecord && typeof renderStreamFromRecord === 'function') {
        renderStreamFromRecord(lastRecord);
      } else if (target === 'raw' && lastRecord && typeof renderRaw === 'function') {
        renderRaw(lastRecord);
      }
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
      // 「经验库」tab：首次进入或再次进入都拉一次最新库内容
      if (tab === 'experience' && typeof initExperienceTab === 'function') {
        initExperienceTab();
      }
      // 「追问」tab：切过去时滚到最新一条
      if (tab === 'chat') {
        const box = $('#chat-messages');
        if (box) box.scrollTop = box.scrollHeight;
      }
      // 合并后的各组面板：把当前激活的子 tab 状态同步到条上
      syncSubtabBar(tab);
      // Phase A Task 1.3：决策 / 决策树 / 预测 tab 切回时重新渲染，避免显示陈旧内容
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
  $('#btn-chat-resend').addEventListener('click', resendLastChat);
  $('#chat-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') sendChat();
  });
  const btnChatClear = $('#btn-chat-clear');
  if (btnChatClear) btnChatClear.addEventListener('click', clearChatOutput);

  // 实时刷新开关：开 = 启动前端轮询流（每 5s 拉 /api/bars）
  $('#cb-live-refresh').addEventListener('change', (e) => {
    if (e.target.checked) {
      startSSEBarsStream();
    } else {
      stopSSEBarsStream();
      stopLiveRefresh();
      updateSSEStatus('off');
    }
  });

  // 等待收盘复选框：勾选时启动倒计时显示，取消时停止
  const cbWaitClose = $('#cb-wait-close');
  if (cbWaitClose) {
    cbWaitClose.addEventListener('change', (e) => {
      if (e.target.checked) {
        // 勾选：立即启动倒计时显示
        startWaitingCountdownDisplay();
      } else {
        // 取消：停止倒计时显示
        stopWaitingCountdownDisplay();
        // 同时必须结束「等待收盘中」的分析流程。之前只停了显示定时器，
        // 若此刻 startAnalysis 正 await 在 startWaitCloseCountdown() 上，
        // resolver 会永远挂着 → 分析永不发起、按钮卡在 waiting/idle。
        // stopWaitCloseCountdown() 会 resolve(false) 让 startAnalysis 正常 return。
        stopWaitCloseCountdown();
      }
      // 同步分析按钮样式（waiting ⇄ idle）
      refreshAnalyzeButtonWaitingState();
    });
  }

  // 持续分析开关：开启时锁定实时+等待收盘，关闭时恢复可编辑
  const cbKeepAnalysis = $('#cb-keep-analysis');
  if (cbKeepAnalysis) {
    cbKeepAnalysis.addEventListener('change', () => {
      const cbLive = $('#cb-live-refresh');
      const cbWait = $('#cb-wait-close');
      if (cbKeepAnalysis.checked) {
        // 持续分析依赖实时 K 线轮询流（本地定时器读刚收盘 bar）+ 等待收盘
        // 强制勾选并锁定「实时」+「等待收盘」
        if (cbLive) { cbLive.checked = true; cbLive.disabled = true; }
        if (cbWait) { cbWait.checked = true; cbWait.disabled = true; }
        // 如果实时之前未开，现在开启轮询流
        if (!barsStreamPolling) startSSEBarsStream();
        // 哨兵对齐到当前已收盘的那根：等**下一根**收盘才分析（与原 SSE 语义一致，
        // 否则本地定时器会在 3s 内对早已收盘的 bar 补跑一轮）
        primeKeepAnalysisSentinel();
        // 启动倒计时显示
        startWaitingCountdownDisplay();
      } else {
        // 关闭持续分析：恢复「实时」「等待收盘」可编辑状态
        if (cbLive) cbLive.disabled = false;
        if (cbWait) cbWait.disabled = false;
        // 如果等待收盘也没勾选，停止倒计时显示
        if (!cbWait || !cbWait.checked) {
          stopWaitingCountdownDisplay();
        }
      }
      // 同步分析按钮样式（waiting ⇄ idle）
      refreshAnalyzeButtonWaitingState();
      // 更新状态栏文案
      if (barsStreamPolling) {
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
        // 跟随系统时整块自定义配置是隐藏的，直接 focus 会聚焦到一个
        // display:none 的输入框（无处可输入）。先翻到「使用我自己的模型」。
        if (!isLlmCustomSelected()) setLlmSourceMode(true);
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
  // 注：原此处监听 $('#ds-symbol-select')，但 index.html 里并不存在该 id
  //（品种输入实际是隐藏域 #ds-symbol + .symbol-search-results 搜索下拉），
  // 属于永不触发的死代码。清除错误提示的逻辑已由搜索结果的选中分支处理。

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

  // 页面可见性优化：隐藏时暂停轮询流，可见时恢复（隐藏的标签页不占后端请求）
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      // 页面隐藏 → 暂停轮询流，降低后台资源占用（隐藏的标签页不发请求）
      if ($('#cb-live-refresh').checked) {
        stopSSEBarsStream();
        stopLiveRefresh();
        updateSSEStatus('paused');
      }
    } else {
      // 页面恢复可见 → 立即拉一次填补空缺，然后重启轮询流
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
    // provider 段：跟随系统时**只提交开关本身**。
    //
    // 为什么不能无条件带上 model/base_url/api_key：`PUT /api/settings` 落库时
    // 「只提交 body 里真正声明过的键」（见 routes_settings.put_settings），
    // 把隐藏字段一并提交，就会把用户从没填过（或早已废弃）的旧值写进用户覆盖区。
    // resolve 时它们会被 use_custom 门控丢弃，但覆盖区里留着一堆无效数据，
    // 且用户切回「自带模型」时会冒出自己从没填过的值。
    const useCustomLlm = isLlmCustomSelected();
    const providerPatch = { use_custom: useCustomLlm };
    if (useCustomLlm) {
      providerPatch.base_url = $('#s-base-url').value;
      providerPatch.model = $('#s-model').value;
      // 掩码值（abcd****wxyz）照常回传：后端 _should_keep_existing 会识别并丢弃它，
      // 真实凭据不被覆盖 —— 该保护机制不在这里也不该被绕过。
      providerPatch.api_key = $('#s-api-key').value;
      providerPatch.reasoning_effort = $('#s-reasoning-effort').value;
      providerPatch.thinking = $('#s-thinking').checked;
      providerPatch.max_output_tokens = parseInt($('#s-max-output-tokens').value) || 0;
      // Phase I Task 19: context_window 在 AIProviderSettings
      providerPatch.context_window = parseInt($('#s-context-window').value) || 2000000;
    }
    await API.put('/api/settings', {
      provider: providerPatch,
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
      // TradingView 凭证（空字符串=匿名访问；保存到 settings.json 后下次切换生效）
      // 优先使用 session_id（从浏览器获取），其次使用账号密码
      tradingview: {
        session_id: ($('#s-tv-session-id')?.value || '').trim(),
        username: ($('#s-tv-username')?.value || '').trim(),
        password: ($('#s-tv-password')?.value || '').trim(),
      },
    });
    $('#settings-modal').classList.add('hidden');
    showToast('设置已保存', 'success');
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
    // 若实时刷新已开启，重启轮询流以应用新的设置
    if ($('#cb-live-refresh').checked) {
      stopSSEBarsStream();
      startSSEBarsStream();
    }
  } catch (e) {
    showToast('保存失败: ' + e.message, 'error');
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
// switch-performance-refactor spec：
//   - _inflightSwitch 防重入
//   - /api/subscribe 15s 超时（AbortController）
//   - subscribe 成功后并行执行 loadBars/loadSettings/loadHistoryList/refreshIncrementalButtonState
//   - loadBars 完成后再 startSSEBarsStream（避免轮询流按旧游标取数）
//   - 失败用 showSwitchError 替代 alert，3 秒后恢复按钮状态
async function applySubscribe() {
  // 重入保护：切换进行中直接忽略后续点击
  if (_inflightSwitch) return;
  const btn = $('#btn-apply-subscribe');
  const symbolInput = $('#ds-symbol');
  const exchangeSelect = $('#ds-exchange');
  const searchInput = $('#ds-symbol-search');
  const symbol = symbolInput.value.trim().toUpperCase();
  const timeframe = $('#ds-timeframe').value;
  const exchange = exchangeSelect.value;
  if (!symbol) {
    showSwitchError('请输入品种代码', 'symbol');
    return;
  }

  _inflightSwitch = true;
  // 切换前若实时刷新开启，先停轮询流，避免按旧游标继续取数
  const wasLiveRefreshOn = $('#cb-live-refresh').checked;
  if (wasLiveRefreshOn) stopSSEBarsStream();
  // 进入 loading 状态：按钮/搜索框/交易所下拉均 disabled
  btn.disabled = true;
  btn.textContent = '切换中…';
  if (searchInput) searchInput.disabled = true;
  exchangeSelect.disabled = true;
  // 先清空所有指标老数据，避免新数据来之前老指标还显示在图上
  if (window._indicatorsAPI) window._indicatorsAPI.clearAllData();

  // 恢复 UI 控件为可编辑状态（不管成功或失败最终都要恢复）
  const restoreControls = () => {
    btn.textContent = '应用';
    btn.disabled = false;
    if (searchInput) searchInput.disabled = false;
    exchangeSelect.disabled = false;
    _inflightSwitch = false;
  };

  try {
    // 如果用户修改了 K线数量，先保存到后端，避免 loadSettings() 覆盖回旧值
    const newBarCount = parseInt($('#ds-bar-count').value) || 100;
    const oldBarCount = currentSettings?.general?.analysis_bar_count || 100;
    if (newBarCount !== oldBarCount) {
      try {
        await API.put('/api/settings', { general: { analysis_bar_count: newBarCount } });
      } catch (e) {
        console.error('applySubscribe: failed to save analysis_bar_count', e);
      }
    }

    await API.post('/api/subscribe', {
      kind: 'tradingview',
      symbol,
      timeframe,
      exchange,
    }, { timeout: 15000 });

    // subscribe 成功后并行执行 4 个请求：loadBars / loadSettings / loadHistoryList / refreshIncrementalButtonState
    // - loadBars 成功后才能 startSSEBarsStream（避免轮询流按旧游标取数），用 .then 链接
    // - 其他三个无依赖，单个失败不影响其他
    const barsPromise = loadBars().then(() => {
      // 时序：loadBars 完成后再启动轮询流，确保新数据已加载且游标已是新的
      //（#ds-symbol/#ds-timeframe/#ds-exchange 在本函数开头就从 DOM 读出用户新选择）
      if (wasLiveRefreshOn) startSSEBarsStream();
    });

    const [barsResult, settingsResult, historyResult, incrementalResult] = await Promise.allSettled([
      barsPromise,
      loadSettings(),
      loadHistoryList(),
      refreshIncrementalButtonState(),
    ]);

    // 切换品种/周期时自动取消持续分析
    const cbKeep = $('#cb-keep-analysis');
    if (cbKeep) cbKeep.checked = false;
    updateLiveRefreshStatus();

    // 该 (exchange, symbol, timeframe) 有历史成功记录 → 提示可用增量分析。
    // 桌面 GUI 在此直接自动跑一次增量；Web 端改为提示，由用户一键触发 ——
    // 每次分析都会调 LLM 且可能推送下单信号，自动触发会在浏览品种时反复烧
    // token 并打扰通知渠道。
    if (incrementalResult.status === 'fulfilled' && incrementalResult.value) {
      showToast(
        `${symbol} ${timeframe} 有历史记录，可点「增量」省约 14.5K tokens`,
        'success'
      );
    }

    // loadBars 失败：K 线渲染失败，提示用户但其他模块已尝试完成
    if (barsResult.status === 'rejected') {
      throw barsResult.reason;
    }
    // 其他三个失败仅记录日志，不阻塞切换流程
    if (settingsResult.status === 'rejected') {
      console.error('applySubscribe: loadSettings failed', settingsResult.reason);
    }
    if (historyResult.status === 'rejected') {
      console.error('applySubscribe: loadHistoryList failed', historyResult.reason);
    }
    if (incrementalResult.status === 'rejected') {
      console.error('applySubscribe: refreshIncrementalButtonState failed', incrementalResult.reason);
    }

    restoreControls();
  } catch (e) {
    const msg = (e && e.message) || String(e);
    // 错误类型判定：优先使用后端返回的 error_type，其次按消息文本兜底分类
    let type = e?.error_type;
    if (!type) {
      const msgLower = msg.toLowerCase();
      if (msg === 'timeout' || /timeout|timed?\s*out/.test(msgLower)) {
        type = 'timeout';
      } else if (/symbol|not\s*found|404|unsupported\s+timeframe/.test(msgLower)) {
        type = 'symbol';
      } else if (/tvDatafeed|tradingview.*connect|connection|network|fetch/.test(msgLower)) {
        type = 'connection';
      } else {
        type = 'connection';
      }
    }

    // 针对性提示文案
    let displayMsg;
    if (type === 'timeout') {
      displayMsg = '切换超时，请重试。';
    } else if (type === 'symbol') {
      displayMsg = '品种无效，请检查代码：' + msg;
    } else {
      // 连接错误：包含 tvDatafeed 未安装的兜底提示
      if (/tvDatafeed|tradingview.*connect|importerror|module/.test(msg.toLowerCase())) {
        displayMsg = '数据源连接失败：缺少 TradingView 数据模块。请执行 pip install git+https://github.com/rongardF/tvdatafeed.git 并重启服务器。';
      } else {
        displayMsg = '数据源连接失败：' + msg + '。请检查网络或尝试其他交易所。';
      }
    }
    showSwitchError(displayMsg, type);

    // 失败 3 秒后恢复按钮状态（让用户看到错误提示后再恢复可点击）
    setTimeout(() => {
      restoreControls();
      // 失败后若之前实时是开启的，恢复轮询流（仍是旧游标）
      if (wasLiveRefreshOn) startSSEBarsStream();
    }, 3000);
  }
}

// ── 切换错误提示组件 ──────────────────────────────────────────────────
// 在工具栏下方 #switch-error 元素中显示错误提示条，3 类错误用不同颜色：
//   - connection（红）：数据源/网络连接失败
//   - symbol（橙）：品种无效
//   - timeout（黄）：切换超时
// 5 秒后自动隐藏；支持手动关闭。
function showSwitchError(msg, type = 'connection') {
  const el = $('#switch-error');
  if (!el) {
    // 兜底：找不到 #switch-error 元素时退回 console.error
    console.error('[switch-error]', type, msg);
    return;
  }
  // 清掉之前的自动隐藏定时器，避免新提示被旧的定时器提前隐藏
  if (_switchErrorTimer) {
    clearTimeout(_switchErrorTimer);
    _switchErrorTimer = null;
  }
  // 设置类型 class（清除旧类型）并填充内容
  el.classList.remove('type-connection', 'type-symbol', 'type-timeout');
  el.classList.add('type-' + type);
  el.innerHTML = ''
    + '<span class="switch-error-msg"></span>'
    + '<button type="button" class="switch-error-close" title="关闭">✕</button>';
  // 用 textContent 写消息，避免 msg 中包含 HTML 被解析
  el.querySelector('.switch-error-msg').textContent = msg;
  el.removeAttribute('hidden');
  // 手动关闭按钮
  el.querySelector('.switch-error-close').addEventListener('click', () => {
    el.setAttribute('hidden', '');
    if (_switchErrorTimer) {
      clearTimeout(_switchErrorTimer);
      _switchErrorTimer = null;
    }
  });
  // 5 秒后自动隐藏
  _switchErrorTimer = setTimeout(() => {
    el.setAttribute('hidden', '');
    _switchErrorTimer = null;
  }, 5000);
}

// ── 增量分析按钮可用性检查 ────────────────────────────────────────────
// 调 GET /api/records?exchange=&symbol=&timeframe=&limit=1 检查是否存在成功
// 记录。无成功记录 → 禁用按钮 + tooltip 提示；有 → 启用按钮。
/** Enable/disable the 增量 button for the current (exchange, symbol, timeframe).
 *
 *  Returns true when a prior successful record exists, i.e. an incremental run
 *  is possible.  The desktop GUI (_check_auto_incremental) auto-*starts* an
 *  incremental run on every symbol switch; we deliberately do not, because each
 *  run costs an LLM call and can now push a Feishu/PushPlus order signal —
 *  auto-firing while the user browses symbols would spend tokens and spam
 *  notifications.  Instead callers surface a hint so the user can one-click it.
 */
async function refreshIncrementalButtonState() {
  // 现在只负责回答一个问题：当前 (exchange, symbol, timeframe) 下有没有
  // 可复用的完整分析记录 —— 有则「分析」自动走增量，没有则走完整分析。
  // 不再有独立的增量按钮需要维护禁用态。
  try {
    const exchange = $('#ds-exchange').value
      || currentSettings?.general?.last_tradingview_exchange || '';
    const symbol = $('#ds-symbol').value
      || currentSettings?.general?.last_symbol || '';
    const timeframe = $('#ds-timeframe').value
      || currentSettings?.general?.last_timeframe || '';
    if (!exchange || !symbol || !timeframe) return false;
    const data = await API.get(
      `/api/records?exchange=${encodeURIComponent(exchange)}&symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&limit=1`
    );
    const reusable = Array.isArray(data) && data.length > 0 && !data[0].has_exception;
    _incrementalReusable = reusable;
    updateAnalyzeButtonHint();
    return reusable;
  } catch (e) {
    _incrementalReusable = false;
    updateAnalyzeButtonHint();
    return false;
  }
}

// 是否走增量：由「强制完整」开关决定；用户不勾选时，有可复用上下文就走增量。
function shouldUseIncremental() {
  if ($('#cb-force-full')?.checked) return false;
  return !!_incrementalReusable;
}

// 让「分析」按钮自己说清楚它会走哪条路，省掉一个含义模糊的独立按钮。
function updateAnalyzeButtonHint() {
  const btn = $('#btn-analyze-toggle');
  if (!btn) return;
  const forced = $('#cb-force-full')?.checked;
  let tip;
  if (forced) {
    tip = '完整分析：重新读取全部 K 线，不复用上次上下文';
  } else if (_incrementalReusable) {
    tip = '增量分析：复用上次分析上下文，只发送新增 K 线（省 token、上下文连贯）';
  } else {
    tip = '完整分析：当前品种/周期没有可复用的历史记录';
  }
  btn.title = tip;
  const text = btn.querySelector('.btn-analyze-text');
  if (text) {
    text.textContent = forced ? '完整分析'
      : (_incrementalReusable ? '增量分析' : '分析');
  }
}

// ── 实时刷新（前端按自己游标的轮询） ────────────────────────────────────
// 唯一调用方是 startSSEBarsStream()，间隔固定为 BARS_POLL_INTERVAL_MS（5000ms）；
// intervalMs 参数保留给潜在的其它调用方，不传则读设置里的 refresh_interval_ms
function startLiveRefresh(intervalMs) {
  stopLiveRefresh();
  const interval = intervalMs || (parseInt($('#s-refresh-ms').value) || 1000);
  // 限制最小 500ms，避免压垮后端
  const safeInterval = Math.max(500, interval);
  liveRefreshTimer = setInterval(refreshBarsOnly, safeInterval);
  liveRefreshLastTs = Date.now();
  // 轮询模式下需要「距下次收盘」倒计时：启动低频拉取 next_close_ts
  // （轮询模式下 next_close_ts 的唯一来源就是它）
  startNextClosePolling();
  updateLiveRefreshStatus();
}

// ── 品种输入模式切换（下拉 ↔ 自定义输入） ────────────────────────────
let symbolInputMode = 'select';  // 'select' or 'custom'
let symbolList = [];              // 品类列表数据，供搜索使用

function toggleSymbolInputMode() {
  const searchInput = $('#ds-symbol-search');
  const input = $('#ds-symbol');
  const btn = $('#btn-symbol-toggle');
  const searchContainer = $('.symbol-search-container');
  if (symbolInputMode === 'select') {
    // 切换到自定义输入
    symbolInputMode = 'custom';
    searchContainer.classList.add('hidden');
    input.classList.remove('hidden');
    input.type = 'text';
    input.value = searchInput.value || '';
    input.size = 15;
    input.focus();
    btn.classList.add('active');
    btn.textContent = '📋';
    btn.title = '切换回搜索选择';
  } else {
    // 切换回搜索
    symbolInputMode = 'select';
    input.classList.add('hidden');
    input.type = 'hidden';
    searchContainer.classList.remove('hidden');
    if (input.value) searchInput.value = input.value;
    btn.classList.remove('active');
    btn.textContent = '✏️';
    btn.title = '切换到自定义输入';
  }
}

// ── 品类搜索功能 ──────────────────────────────────────────────────────
let symbolSearchSelectedIndex = 0;
let symbolMatchTotal = 0;

function parseSymbol(symbol) {
  if (typeof symbol === 'object' && symbol !== null) {
    return { code: symbol.code || '', name: symbol.name || '', full: symbol.code || '' };
  }
  const parts = symbol.split(' ');
  if (parts.length >= 2) {
    const code = parts[0];
    const name = parts.slice(1).join(' ');
    return { code, name, full: symbol };
  }
  return { code: symbol, name: '', full: symbol };
}

function handleSymbolSearch(e) {
  const query = e.target.value.trim();
  const clearBtn = $('#btn-symbol-clear');
  if (clearBtn) {
    clearBtn.toggleAttribute('hidden', !query);
  }
  filterSymbolList(query);
}

// ── 模糊搜索 ────────────────────────────────────────────────────────────────
// 之前只做 code/name 的 includes 子串匹配，输「btc」命中不了「比特币」，
// 输「nas」也只能靠运气。加上：
//   1) 子串匹配（大小写无关）
//   2) 子序列匹配（b-t-c 命中 BTCUSDT、苹-果 命中 苹果）
//   3) 匹配得分排序：前缀 > 子串 > 子序列，同分再按预设顺序
// 空查询时按分组展示「常用 / 全部」，而不是把一长串平铺。

function _norm(s) { return String(s || '').toLowerCase(); }

function _subseqMatch(needle, haystack) {
  // 子序列匹配，返回得分（越靠前分越高）或 null
  let hi = 0, score = 0, lastIdx = -1;
  for (let i = 0; i < needle.length; i++) {
    const idx = haystack.indexOf(needle[i], hi);
    if (idx === -1) return null;
    score += Math.max(1, 8 - (idx - lastIdx - 1) * 2);
    lastIdx = idx;
    hi = idx + 1;
  }
  return score;
}

function scoreSymbol(query, symbol) {
  const { code, name } = parseSymbol(symbol);
  const q = _norm(query);
  if (!q) return { score: 0, tier: 0 };
  const c = _norm(code), n = _norm(name);
  if (c === q || n === q) return { score: 1000, tier: 0 };
  if (c.startsWith(q)) return { score: 900 - c.length, tier: 0 };
  if (n.startsWith(q)) return { score: 850 - n.length, tier: 0 };
  if (c.includes(q)) return { score: 700 - c.indexOf(q), tier: 1 };
  if (n.includes(q)) return { score: 650 - n.indexOf(q), tier: 1 };
  const cs = _subseqMatch(q, c);
  if (cs != null) return { score: 400 + cs, tier: 2 };
  const ns = _subseqMatch(q, n);
  if (ns != null) return { score: 300 + ns, tier: 2 };
  return null;
}

const SYMBOL_GROUPS = [
  { key: 'crypto',  label: '加密货币', test: s => /USDT|USD$|BTC-|ETH-/.test(s) || /USDT$/.test(s) },
  { key: 'forex',   label: '外汇 / 贵金属', test: s => /^(XAU|XAG|XPT|EUR|GBP|USD|AUD|NZD|CAD|CHF|JPY|MXN|ZAR|SGD|HKD)/.test(s) },
  { key: 'usstock', label: '美股 / 指数', test: s => /^[A-Z]{1,5}$/.test(s) || /^(SPX|NDX|VIX|DJI)/.test(s) },
  { key: 'cn',      label: 'A 股', test: s => /^(6|0|3|2)\\d{5}$/.test(s) },
  { key: 'hk',      label: '港股', test: s => /^\\d{4}$/.test(s) },
  { key: 'futures', label: '期货', test: s => /^(ZC|ZS|ZW|ZL|ZM|ZO|LE|GF|HE|ES|NQ|YM|RTY|CL|NG|GC|SI)$/.test(s) },
];

function groupOfSymbol(code) {
  const c = String(code || '');
  for (const g of SYMBOL_GROUPS) { if (g.test(c)) return g.label; }
  return '其他';
}

// ── 在线搜索（TradingView scanner）──────────────────────────────────────────
// /api/tv/symbols 只是离线内置表（195 个），用户要的「随便搜哪个币种」需要
// 全市场实时检索：scanner 返回 crypto 64k / america 20k / futures 52k 个品种。
// 策略：空查询用内置表（瞬时、离线可用、带中文名与分组），
// 有输入时防抖 260ms 走在线搜索；在线失败或无结果则回退到内置表模糊匹配。

const SYMBOL_SEARCH_DEBOUNCE_MS = 260;
let _symbolSearchTimer = null;
let _symbolSearchSeq = 0;   // 竞态守卫：只认最后一次输入的结果

async function fetchRemoteSymbols(query) {
  const ex = $('#ds-exchange')?.value || '';
  const seq = ++_symbolSearchSeq;
  try {
    const d = await API.get(
      `/api/tv/search?q=${encodeURIComponent(query)}&exchange=${encodeURIComponent(ex)}&limit=50`
    );
    if (seq !== _symbolSearchSeq) return;      // 已有更新的输入，丢弃
    if (d && Array.isArray(d.results) && d.results.length) {
      renderSymbolResults(d.results.map(r => ({
        sym: { code: r.code, name: r.name || r.description || r.code },
        group: null,
        sub: r.exchange || ex,
      })));
    } else {
      _renderLocalMatch(query);
    }
  } catch (e) {
    if (seq !== _symbolSearchSeq) return;
    _renderLocalMatch(query);   // 在线不可用 → 内置表模糊匹配兜底
  }
}

function _renderLocalMatch(query) {
  const scored = [];
  symbolList.forEach((sym, i) => {
    const r = scoreSymbol(query, sym);
    if (r) scored.push({ sym, score: r.score, tier: r.tier, i });
  });
  scored.sort((a, b) => (a.tier - b.tier) || (b.score - a.score) || (a.i - b.i));
  symbolMatchTotal = scored.length;
  renderSymbolResults(scored.map(x => ({ sym: x.sym, group: null })));
}

function filterSymbolList(query) {
  const dropdown = $('#symbol-search-dropdown');
  const resultsContainer = $('.symbol-search-results');
  if (!dropdown || !resultsContainer) return;

  // 有输入 → 走在线全市场搜索
  if (query) {
    if (_symbolSearchTimer) clearTimeout(_symbolSearchTimer);
    symbolSearchSelectedIndex = 0;
    resultsContainer.innerHTML = '<div class="symbol-search-hint">搜索中…</div>';
    dropdown.removeAttribute('hidden');
    _symbolSearchTimer = setTimeout(() => fetchRemoteSymbols(query), SYMBOL_SEARCH_DEBOUNCE_MS);
    return;
  }

  let rows;
  if (query) {
    const scored = [];
    symbolList.forEach((sym, i) => {
      const r = scoreSymbol(query, sym);
      if (r) scored.push({ sym, score: r.score, tier: r.tier, i });
    });
    scored.sort((a, b) => (a.tier - b.tier) || (b.score - a.score) || (a.i - b.i));
    rows = scored.map(x => ({ sym: x.sym, group: null }));
    symbolMatchTotal = scored.length;
  } else {
    // 空查询：按分组展示，前 12 个视为「常用」
    rows = symbolList.slice(0, 12).map(sym => ({ sym, group: '常用' }));
    symbolList.slice(12).forEach(sym => rows.push({ sym, group: groupOfSymbol(sym) }));
    symbolMatchTotal = symbolList.length;
  }

  symbolSearchSelectedIndex = 0;
  renderSymbolResults(rows);
  dropdown.removeAttribute('hidden');
}

function showSymbolDropdown() {
  const dropdown = $('#symbol-search-dropdown');
  if (!dropdown) return;
  dropdown.removeAttribute('hidden');
  // 聚焦一律展示**浏览清单**（常用 + 分类），而不是拿框里的值去搜。
  // 此前聚焦会搜索框内已有的当前品种，只返回寥寥几条 —— 看起来像功能坏了，
  // 而那份分组清单只能靠点「清空」才够得着。输入才会切到搜索。
  filterSymbolList('');
}

function renderSymbolResults(rows) {
  const container = $('.symbol-search-results');
  if (!container) return;

  if (!rows.length) {
    container.innerHTML = '<div class="symbol-search-no-results">未找到匹配的品类 —— '
      + '可直接输入代码并点「应用」手工订阅</div>';
    return;
  }

  const maxResults = 60;
  const shown = rows.slice(0, maxResults);
  const parts = [];
  if (rows.length > maxResults) {
    parts.push(`<div class="symbol-search-hint">共 ${symbolMatchTotal} 个匹配，显示前 ${maxResults} 个；可继续输入以缩小范围</div>`);
  }

  let currentGroup = null;
  let idx = 0;
  for (const row of shown) {
    const { code, name } = parseSymbol(row.sym);
    const isSelected = idx === symbolSearchSelectedIndex;
    if (row.group && row.group !== currentGroup) {
      currentGroup = row.group;
      parts.push(`<div class="symbol-search-group">${escapeHtml(currentGroup)}</div>`);
    }
    // 用 data 属性 + 事件委托，不做字符串插值内联 onclick
    parts.push(`
      <div class="symbol-search-item${isSelected ? ' selected' : ''}"
           data-symbol="${escapeHtml(code)}" data-index="${idx}" role="option">
        <span class="symbol-name">${escapeHtml(name || code)}</span>
        <span class="symbol-code">${escapeHtml(row.sub ? escapeHtml(row.sub) + ' · ' : '')}${escapeHtml(code)}</span>
      </div>`);
    idx += 1;
  }
  container.innerHTML = parts.join('');
}

function selectSymbol(symbol) {
  const searchInput = $('#ds-symbol-search');
  const hiddenInput = $('#ds-symbol');
  const dropdown = $('#symbol-search-dropdown');

  if (searchInput) searchInput.value = symbol;
  if (hiddenInput) hiddenInput.value = symbol;
  if (dropdown) dropdown.setAttribute('hidden', '');
  _expSelectedSymbol = symbol;

  const alert = $('#symbol-alert');
  if (alert) alert.setAttribute('hidden', '');
}

function clearSymbolSearch() {
  const searchInput = $('#ds-symbol-search');
  const clearBtn = $('#btn-symbol-clear');
  if (searchInput) {
    searchInput.value = '';
    searchInput.focus();
  }
  if (clearBtn) {
    clearBtn.setAttribute('hidden', '');
  }
  filterSymbolList('');
}

function handleSymbolSearchKeydown(e) {
  const dropdown = $('#symbol-search-dropdown');
  if (!dropdown || dropdown.hasAttribute('hidden')) return;

  const results = dropdown.querySelectorAll('.symbol-search-item');
  if (results.length === 0) return;

  switch (e.key) {
    case 'ArrowDown':
      e.preventDefault();
      symbolSearchSelectedIndex = (symbolSearchSelectedIndex + 1) % results.length;
      updateSymbolSearchSelection(results);
      break;
    case 'ArrowUp':
      e.preventDefault();
      symbolSearchSelectedIndex = (symbolSearchSelectedIndex - 1 + results.length) % results.length;
      updateSymbolSearchSelection(results);
      break;
    case 'Enter':
      e.preventDefault();
      if (results[symbolSearchSelectedIndex]) {
        const symbol = results[symbolSearchSelectedIndex].dataset.symbol;
        selectSymbol(symbol);
      }
      break;
    case 'Escape':
      e.preventDefault();
      dropdown.setAttribute('hidden', '');
      break;
  }
}

function updateSymbolSearchSelection(results) {
  results.forEach((item, index) => {
    if (index === symbolSearchSelectedIndex) {
      item.classList.add('selected');
      item.scrollIntoView({ block: 'nearest' });
    } else {
      item.classList.remove('selected');
    }
  });
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
  // 守卫：轮询流活跃时由 updateSSEStatusWithExpiry 接管状态栏文案，
  // 此处直接返回避免覆盖「距上次刷新 · 距下次收盘」文案
  if (barsStreamPolling) {
    return;
  }
  const cbKeep = $('#cb-keep-analysis');
  const keepSuffix = (cbKeep && cbKeep.checked) ? ' · 持续分析中' : '';
  const el = $('#live-refresh-status');
  if (!el) return;

  // 轮询流不活跃（实时关闭 / demo / 回看）时也显示「距下次收盘」
  //（由 startNextClosePolling 维护 sseNextCloseTs）
  let closeText = '';
  if (sseNextCloseTs > 0) {
    const dsTf = $('#ds-timeframe')?.value || '';
    const tfSecs = timeframeToSeconds(dsTf || currentSettings?.general?.last_timeframe || '');
    if (tfSecs > 0) {
      const remaining = Math.max(0, (sseNextCloseTs - Date.now()) / 1000);
      if (remaining <= tfSecs) {
        closeText = ` · 距下次收盘 ${formatCountdownHMS(remaining)}`;
      }
    }
  }

  if (!liveRefreshTimer) {
    el.textContent = keepSuffix ? keepSuffix.replace(/^ · /, '') : '';
    if (!el.textContent) el.style.color = '';
    return;
  }
  const elapsed = liveRefreshLastTs ? Math.max(0, Date.now() - liveRefreshLastTs) : 0;
  el.textContent = `· 距上次刷新 ${(elapsed / 1000).toFixed(1)}s${closeText}${keepSuffix}`;
}

// 每秒更新一次"距上次刷新"显示
setInterval(updateLiveRefreshStatus, 1000);

// ── 实时 K 线流：前端按自己游标轮询（原 /api/bars/stream SSE 已下线） ──────
// 历史：服务端 SSE 从**全局**订阅拉一次 K 线再广播给所有连接，两个标签页看到
// 的是同一条流。「按游标分组广播」被否决 —— EventSource 无法设置请求头，服务端
// 拿不到 X-Session-Id；且一个坏品种的 auto-probe 会长时间持有
// TradingViewSource._snapshot_lock，全站 /api/bars 排队。
// 现在：每个标签页按**自己的** #ds-symbol/#ds-exchange/#ds-timeframe 轮询。
//   · 间隔 BARS_POLL_INTERVAL_MS = 5000ms，对齐原服务端 SSE 推送节奏
//   · 隐藏标签页由 visibilitychange 自动停，坏 tab 不传染别人
//   · next_close_ts 由 startNextClosePolling 低频拉取（唯一来源）
//
// ⚠ 调用前必须先写好 #ds-symbol/#ds-exchange/#ds-timeframe：startNextClosePolling
// 会**同步**发起一次 next-close 请求，读到的必须是新游标而不是旧游标。
function startSSEBarsStream() {
  stopSSEBarsStream();
  barsStreamPolling = true;
  updateSSEStatus('ok');
  // 共享 tick：状态栏「距上次刷新 · 距下次收盘」+ 「等待收盘」按钮倒计时
  startSSEStatusExpiryTimer();
  // 持续分析的收盘触发（本地定时器，替代原 bar_close SSE 事件）
  startKeepAnalysisTimer();
  // K 线轮询（内部同时启动 5s 的 next-close 拉取）
  startLiveRefresh(BARS_POLL_INTERVAL_MS);
  // 首次启动且尚无数据时立即补拉一次（切换失败后恢复实时等场景）
  if (!lastBars || !lastBars.length) refreshBarsOnly();
  updateSSEStatusWithExpiry();
}

function stopSSEBarsStream() {
  barsStreamPolling = false;
  stopKeepAnalysisTimer();
  // 无条件停：轮询流本身就是 liveRefreshTimer
  stopLiveRefresh();
  stopSSEStatusExpiryTimer();
  stopNextClosePolling();
  sseNextCloseTs = 0;
  marketClosed = false;
}

// ── 持续分析：本地定时触发（替代原 bar_close SSE 事件） ─────────────────
// K 线轮询每 5s 刷新一次 lastBars；本定时器只读本地数据、不发请求，
// 发现「刚收盘的那根 bar」变了就自动发起下一轮分析。
//   · 判定复用 PAContinuousGate.closedBarTs()（纯函数，Node 单测守护）
//   · triggerSource 必须传 'continuous'：本次调用本身就是收盘后触发的，
//     bar 刚刚收盘，再等一根必然出错（AGENTS.md 硬约束）
//   · 休市时 closedBarTs 恒定不变 → 哨兵去重天然抑制重复触发
function startKeepAnalysisTimer() {
  if (keepAnalysisTimer) return;
  keepAnalysisTimer = setInterval(maybeTriggerContinuousAnalysis, KEEP_ANALYSIS_TICK_MS);
}

function stopKeepAnalysisTimer() {
  if (keepAnalysisTimer) {
    clearInterval(keepAnalysisTimer);
    keepAnalysisTimer = null;
  }
}

// 把哨兵对齐到「当前已收盘的那根」：开启持续分析后等**下一根**收盘再分析。
// 与原 SSE 语义一致 —— 原来 tick 勾选后要等下一个 bar_close 事件才触发，
// 若不预置哨兵，轮询定时器会在 3s 内对早已收盘的 bar 补跑一轮。
function primeKeepAnalysisSentinel() {
  const ts = window.PAContinuousGate
    ? window.PAContinuousGate.closedBarTs(lastBars)
    : 0;
  if (ts) keepAnalysisLastClosedTs = ts;
}

function maybeTriggerContinuousAnalysis() {
  if (!barsStreamPolling || document.hidden) return;
  const cbKeep = $('#cb-keep-analysis');
  if (!cbKeep || !cbKeep.checked) return;
  if (!lastBars || !lastBars.length) return;
  // 手动「等待收盘」倒计时在途：那一轮分析自己就会在收盘后发起，
  // 这里再触发一次会对同一根 bar 重复分析
  if (waitCloseCountdownResolver) return;
  // 防御性重置：分析流已结束但 isAnalyzing 未被正确重置（边界情况）
  if (isAnalyzing && currentAnalysisStream === null) {
    isAnalyzing = false;
  }
  if (isAnalyzing) return;
  const newClosedTs = window.PAContinuousGate
    ? window.PAContinuousGate.closedBarTs(lastBars)
    : 0;
  if (!newClosedTs || newClosedTs === keepAnalysisLastClosedTs) return;
  keepAnalysisLastClosedTs = newClosedTs;
  // 自动选路：有可复用上下文走增量，否则走完整分析
  if (shouldUseIncremental()) {
    startIncrementalAnalysis(true, 'continuous');
  } else {
    startAnalysis(true, 'continuous');
  }
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

// 异步拉取 /api/bars/next-close 更新 sseNextCloseTs。
// 轮询流下这是 next_close_ts 的**唯一来源**（原 SSE 事件已下线），故不再有
// 「SSE 活跃就跳过」的短路；幂等，并发请求时直接 return。失败静默（下次定时器再试）。
// force=true 时立即绕过 nextClosePollingInFlight 在途节流（倒计时归零等场景用）
async function fetchAndUpdateNextCloseTs(force = false) {
  // force=true 时不遵守「在途节流」：调用方（等待收盘倒计时、sanity check）需要
  // 立刻拿到值，而轮询模式下 next-close 每 5s 都在飞，等一拍会直接返回空值，
  // 导致倒计时拿不到 remaining。
  if (nextClosePollingInFlight && !force) return;
  const ownsThrottle = !nextClosePollingInFlight;
  nextClosePollingInFlight = true;
  try {
    const symbol = $('#ds-symbol')?.value || currentSettings?.general?.last_symbol || '';
    const tf = $('#ds-timeframe')?.value || currentSettings?.general?.last_timeframe || '';
    // 交易所与上面两轴走同一个解析口：DOM 有合法选中就用它，否则回落到服务端游标。
    // 不能只看 `.value`：下拉框在「自动（探测）」（option value=""）或选项尚未
    // 就位时它是空串，直接发出去等于告诉后端「交易所不限」。
    const ex = cursorValue($('#ds-exchange'), currentSettings?.general?.last_tradingview_exchange);
    if (!symbol || !tf) return;
    const url = `/api/bars/next-close?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(tf)}&exchange=${encodeURIComponent(ex)}`;
    const data = await API.get(url);
    if (data && data.market_closed) {
      // 休市：清空 sseNextCloseTs，触发「休市中」显示，避免取模算法返回错误未来值
      sseNextCloseTs = 0;
      marketClosed = true;
    } else {
      marketClosed = false;
      if (data && data.next_close_ts > 0) {
        sseNextCloseTs = Number(data.next_close_ts);
      } else {
        // 后端返回 null（无 forming bar 或 timeframe 无效），清空避免残留旧值
        sseNextCloseTs = 0;
      }
    }
  } catch (e) {
    // 静默失败，下次定时器再试
  } finally {
    if (ownsThrottle) nextClosePollingInFlight = false;
  }
}

// 启动低频（5s）拉取 next_close_ts 定时器（幂等）
// 轮询流下 next_close_ts 的唯一来源，保证「距下次收盘」倒计时可用
function startNextClosePolling() {
  if (nextClosePollingTimer) return;
  // 立即拉一次，避免首次显示延迟 5s
  fetchAndUpdateNextCloseTs();
  nextClosePollingTimer = setInterval(fetchAndUpdateNextCloseTs, 5000);
}

function stopNextClosePolling() {
  if (nextClosePollingTimer) {
    clearInterval(nextClosePollingTimer);
    nextClosePollingTimer = null;
  }
}

// timeframe 字符串 → 秒数（与后端 bar_close_wait.timeframe_to_seconds 对齐）
// 用于状态栏倒计时 sanity check，防止 next_close_ts 过期/竞态时显示异常值
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

// 将秒数格式化为 HH:MM:SS 时分秒格式（不足 1 小时显示 00 开头）
function formatCountdownHMS(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(h)}:${pad(m)}:${pad(sec)}`;
}

// 计算并更新 #live-refresh-status 文案：● 实时轮询 · 距上次刷新 Ns · 距下次收盘 Ms
// 仅在轮询流活跃（barsStreamPolling）时由 sseStatusExpiryTimer 每秒调用；
// 同时承担「等待收盘」共享倒计时（见下方 waitCloseCountdownResolver 分支）
function updateSSEStatusWithExpiry() {
  // 守卫：轮询流不活跃时直接 return（由 updateLiveRefreshStatus 接管）
  if (!barsStreamPolling) return;
  const el = $('#live-refresh-status');
  if (!el) return;
  const elapsed = liveRefreshLastTs ? Math.max(0, (Date.now() - liveRefreshLastTs) / 1000) : 0;
  // sanity check：remaining 不能超过当前 timeframe 的 duration
  // 防止 next_close_ts 过期、时区偏移或跨周期残留导致显示异常大的倒计时
  // 优先从 #ds-timeframe 读用户实际选择的值（applySubscribe 切换后 currentSettings 可能未同步），
  // fallback 到 currentSettings.general.last_timeframe
  const dsTf = $('#ds-timeframe')?.value || '';
  const tfSecs = timeframeToSeconds(dsTf || currentSettings?.general?.last_timeframe || '');
  let remaining = 0;
  let closeText = '';
  if (sseNextCloseTs > 0 && tfSecs > 0) {
    remaining = Math.max(0, (sseNextCloseTs - Date.now()) / 1000);
    // 若 remaining 超过 1 个 timeframe duration，说明 next_close_ts 已过期或错误，丢弃
    if (remaining > tfSecs) {
      console.warn('[bars] next_close_ts sanity check failed, fetching /api/bars/next-close:', {
        sseNextCloseTs, tfSecs, remaining,
        now: Date.now()
      });
      // fallback: 强制拉取正确的 next_close_ts
      fetchAndUpdateNextCloseTs(true);
      sseNextCloseTs = 0;  // 清除错误值，等待 fallback 更新
    } else {
      closeText = ` · 距下次收盘 ${formatCountdownHMS(remaining)}`;
    }
  }
  // 休市检测：next-close 端点 bars[0].closed == true 时返回 market_closed（此时
  // 取模算法会基于过期 ts_open 返回错误的未来周期边界，必须短路），主动置位。
  // 次级信号：next_close_ts 为 0 且超过 1 个周期没有 bar 更新 → 也判定为休市。
  if (!closeText && tfSecs > 0) {
    if (marketClosed) {
      closeText = ' · 休市中';
    } else if (sseNextCloseTs > 0 && sseNextCloseTs < Date.now()) {
      // next_close_ts 已过期但非 0：主动再拉一次纠正（跨越周期边界时的竞态）
      fetchAndUpdateNextCloseTs(true);
      sseNextCloseTs = 0;  // 清除过期值，等待 REST 返回正确状态
    } else if (sseNextCloseTs === 0) {
      const sinceUpdate = liveRefreshLastTs ? (Date.now() - liveRefreshLastTs) / 1000 : 0;
      if (sinceUpdate > tfSecs) {
        closeText = ' · 休市中';
      }
    }
  }
  let text = `● 实时轮询 · 距上次刷新 ${elapsed.toFixed(1)}s${closeText}`;
  // 持续分析后缀
  const cbKeep = $('#cb-keep-analysis');
  if (cbKeep && cbKeep.checked) text += ' · 持续分析中';
  el.textContent = text;
  el.style.color = '#26a69a';

  // 共享倒计时：同时更新「等待收盘」按钮（与状态栏同一 tick、同一 remaining，
  // 彻底消除两个独立 setInterval 导致的不同步和算法不一致问题）
  if (waitCloseCountdownResolver) {
    const btn = $('#btn-analyze-toggle');
    if (!btn || btn.dataset.state !== 'waiting') return;
    // 用户中途取消「等待收盘」勾选 → resolve(false) 终止等待
    const cb = $('#cb-wait-close');
    if (cb && !cb.checked) {
      const r = waitCloseCountdownResolver;
      waitCloseCountdownResolver = null;
      stopWaitCloseCountdown();
      r(false);
      return;
    }
    // 倒计时归零（sseNextCloseTs 已到当前时间）→ 刷新 K线后 resolve(true) 触发分析
    if (sseNextCloseTs > 0 && remaining <= 0) {
      const r = waitCloseCountdownResolver;
      waitCloseCountdownResolver = null;
      stopWaitCloseCountdown();
      loadBars()
        .then(() => {
          // 更新持续分析去重哨兵（与 maybeTriggerContinuousAnalysis 同一实现，避免两处公式漂移）
          const newClosedTs = window.PAContinuousGate
            ? window.PAContinuousGate.closedBarTs(lastBars)
            : 0;
          if (newClosedTs) keepAnalysisLastClosedTs = newClosedTs;
        })
        .catch((e) => console.error('waitCloseCountdown loadBars:', e))
        .finally(() => { if (r) r(true); });
      return;
    }
    // 仍在等待：用同一个 remaining 更新按钮文本（与状态栏完全同步）
    if (remaining > 0) {
      updateWaitingButtonCountdown(Math.ceil(remaining));
    }
  }
}

// 更新实时流状态指示器（#live-refresh-status）
// state: 'ok' | 'connecting' | 'fallback' | 'paused' | 'off'
function updateSSEStatus(state) {
  const el = $('#live-refresh-status');
  if (!el) return;
  let base = '';
  switch (state) {
    case 'ok':
      base = '● 实时轮询';
      el.style.color = '#26a69a';
      break;
    case 'connecting':
      base = '○ 连接中...';
      el.style.color = '#ffc800';
      break;
    case 'fallback':
      base = '⚠ 实时轮询已中断';
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
  // 追加持续分析标记
  const cbKeep = $('#cb-keep-analysis');
  if (cbKeep && cbKeep.checked && base) {
    base = base + ' · 持续分析中';
  }
  el.textContent = base;
}

// ── Analysis ───────────────────────────────────────────────────────────

// 分析按钮状态机：idle → analyzing → idle / waiting
//   idle:      蓝色 ▶ 分析（点击开始）
//   analyzing: 红色 ⏹ 取消（脉动动画，点击中止）
//   waiting:   青绿色 ⏳ 等待收盘（等待收盘/持续分析启用时的视觉提示，仅样式变化，点击仍触发分析）
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
  } else if (state === 'waiting') {
    // 等待收盘状态：仅样式变化，按钮仍可点击手动触发分析
    btn.disabled = false;
    btn.title = '等待 K 线收盘后自动分析（可点击立即手动分析）';
    if (iconEl) iconEl.textContent = '⏳';
    if (textEl) textEl.textContent = '等待收盘';
  }
}

// 更新等待按钮的倒计时文本
function updateWaitingButtonCountdown(remainingSeconds) {
  const btn = $('#btn-analyze-toggle');
  if (!btn || btn.dataset.state !== 'waiting') return;
  const textEl = btn.querySelector('.btn-analyze-text');
  if (textEl) {
    textEl.textContent = `等待收盘 ${formatCountdownHMS(remainingSeconds)}`;
  }
}

// 根据「等待收盘」/「持续分析」开关状态刷新分析按钮样式
// 当任一开关启用且当前未在分析中时，按钮切换为 waiting 样式
// 否则恢复 idle 样式
function refreshAnalyzeButtonWaitingState() {
  if (isAnalyzing) return;  // 分析中保持 analyzing 状态
  const cbWait = $('#cb-wait-close');
  const cbKeep = $('#cb-keep-analysis');
  const waiting = (cbWait && cbWait.checked) || (cbKeep && cbKeep.checked);
  setAnalyzeButtonState(waiting ? 'waiting' : 'idle');
}

// ── FlowBar 5 步进度条（Phase J Task 20） ─────────────────────────────
// 根据 SSE 事件实时切换 6 个 step 的 active/done 状态：
//   1=等待数据 / 2=阶段一推理 / 3=阶段一验证 / 4=阶段二推理 / 5=阶段二验证 / 6=完成
function setFlowBarStep(step) {
  const bar = $('#flow-bar');
  if (!bar) return;
  bar.querySelectorAll('.flow-step').forEach(el => {
    const s = parseInt(el.dataset.step);
    el.classList.remove('active', 'done', 'failed', 'skipped');
    if (s < step) el.classList.add('done');
    else if (s === step) el.classList.add('active');
  });
}

// 标记失败步：failedStep 及之后的所有步骤标记为 failed，之前的标记为 done
// 用于 Stage1Failed / Stage2Failed 事件，避免 done 事件错误地推进到完成步
function setFlowBarFailed(failedStep) {
  const bar = $('#flow-bar');
  if (!bar) return;
  bar.querySelectorAll('.flow-step').forEach(el => {
    const s = parseInt(el.dataset.step);
    el.classList.remove('active', 'done', 'failed', 'skipped');
    if (s < failedStep) el.classList.add('done');
    else el.classList.add('failed');
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
  const btnToggle = $('#btn-sidebar-toggle');
  if (!sb) return;
  if (collapsed) {
    sb.classList.add('collapsed');
    if (fab) fab.classList.add('visible');
    // 按钮图标联动：折叠后显示「展开」图标 + 提示
    if (btnToggle) {
      btnToggle.textContent = '📥';
      btnToggle.title = '展开侧边栏';
      btnToggle.classList.add('is-collapsed');
    }
  } else {
    sb.classList.remove('collapsed');
    if (fab) fab.classList.remove('visible');
    // 按钮图标联动：展开后显示「收起」图标 + 提示
    if (btnToggle) {
      btnToggle.textContent = '📊';
      btnToggle.title = '隐藏侧边栏';
      btnToggle.classList.remove('is-collapsed');
    }
  }

  // 🔑 关键：强制重置 chart-main 的 inline width，让 flex 布局重新计算
  // LightweightCharts 内部会设置固定 inline width，阻止 flex:1 自动扩展
  const chartMainEl = document.getElementById('chart-main');
  if (chartMainEl) {
    chartMainEl.style.width = '100%';
  }

  // CSS transition 时长 50ms（极短，避免 TRAE webview 的 rAF throttle 问题）；
  // rAF 循环跑 150ms 兜底，确保 chart 跟上最终宽度。
  // 另外在每次 resizeChart 前强制 reflow（读 offsetWidth 触发布局同步），
  // 确保即使 webview throttle rAF，chart 也能拿到最新宽度。
  const TRANSITION_MS = 150;
  const start = performance.now();
  function tick() {
    // 强制同步布局：读 offsetWidth 会触发 reflow，确保后续 clientWidth 读到最新值
    const pane = document.getElementById('chart-pane');
    if (pane) void pane.offsetWidth;
    if (typeof resizeChart === 'function') resizeChart();
    if (performance.now() - start < TRANSITION_MS) {
      requestAnimationFrame(tick);
    } else {
      // 最后一次确保最终尺寸对齐
      if (typeof resizeChart === 'function') resizeChart();
    }
  }
  requestAnimationFrame(tick);
}

// 「重试后取消持续跟踪」开关（对齐桌面 GUI MainWindow._on_retry_occurred）。
// 此前 Web 端只在保存设置时写盘，从未在重试事件里读取该开关 —— 界面上是一个
// 完全不起作用的复选框。重试说明本轮数据/响应不稳定，继续自动跟随收盘重跑
// 很可能连续失败并烧 token，故按开关自动关闭并持久化。
async function applyCancelKeepAnalysisOnRetry(stageLabel) {
  if (currentSettings?.general?.cancel_keep_analysis_on_retry !== true) return;
  const cbKeep = $('#cb-keep-analysis');
  const cbSetting = $('#s-keep-analysis');
  if (!cbKeep || !cbKeep.checked) return;  // 未开启持续分析，无需处理

  cbKeep.checked = false;
  if (cbSetting) cbSetting.checked = false;
  updateLiveRefreshStatus();
  try {
    await API.put('/api/settings', { general: { keep_analysis: false } });
    if (currentSettings?.general) currentSettings.general.keep_analysis = false;
    showToast(`持续跟踪分析已因${stageLabel || '模型'}重试自动关闭`, 'warning');
  } catch (err) {
    console.error('cancel keep_analysis on retry: persist failed', err);
  }
}

async function startAnalysis(continuousMode = false, triggerSource = 'user') {
  // 等待收盘：若勾选了「等待收盘」复选框，先调 /api/bars/next-close 拿到剩余
  // 秒数，每秒更新倒计时，归零后再实际发起 /api/analyze/stream 请求。
  //
  // triggerSource='continuous' 表示本次是由「刚收盘」触发的（bar 刚刚收盘），
  // 此时绝不能再等一根 —— 否则分析整整晚一个周期，且下一次收盘触发会把
  // pending 的 resolver resolve(false) 取消掉，表现为持续分析反复重置、永不
  // 真正发起分析。详见 static/js/continuous_gate.js。
  const cbWaitClose = $('#cb-wait-close');
  const needWait = window.PAContinuousGate
    ? window.PAContinuousGate.shouldWaitForClose(triggerSource, cbWaitClose && cbWaitClose.checked)
    : !!(cbWaitClose && cbWaitClose.checked);
  if (needWait) {
    const started = await startWaitCloseCountdown();
    if (!started) {
      // 用户在等待期间取消勾选，或后端无法获取剩余秒数 → 不发分析请求
      return;
    }
  }

  setAnalyzeButtonState('analyzing');
  isAnalyzing = true;
  chartUpdatePaused = true;
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
  const continuousParam = continuousMode ? '&continuous=true' : '';

  try {
    const { controller, source } = API.sse(`/api/analyze/stream?bar_count=${barCount}${continuousParam}`);
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
              applyCancelKeepAnalysisOnRetry('阶段一');
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
              // FlowBar：标记阶段一推理步及之后所有步为失败，避免 done 事件错误推进
              setFlowBarFailed(2);
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
              applyCancelKeepAnalysisOnRetry('阶段二');
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
              // FlowBar：标记阶段二推理步及之后所有步为失败
              setFlowBarFailed(4);
              break;
            case 'InsufficientData':
              stage = '⚠️ 数据不足，无法分析';
              if (evt.reason) stage += `：${evt.reason}`;
              // FlowBar：数据不足在阶段一推理之前，标记第 1 步之后全部失败
              setFlowBarFailed(2);
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
          // 主图 fitView 后同步副图时间轴（确保副图跟随主图逻辑范围）
          if (window._indicatorsAPI && window._indicatorsAPI.syncSubCharts) {
            window._indicatorsAPI.syncSubCharts();
          }
          enableChat();
          // 分析完成后刷新增量分析按钮可用性（新记录可被增量复用）
          refreshIncrementalButtonState();
          // 下单机会提醒（Phase E Task 12）：Toast + 浏览器通知 + 蜂鸣音
          // 触发条件：order_type ∈ [limit, market, stop] 且 trade_confidence >= 阈值
          // 受 settings.general.alert_on_order_opportunity 开关控制（默认 true，!== false 才触发）
          triggerOrderAlertIfNeeded(evt.record);
          // FlowBar：仅在无异常时标记完成步；异常时保留失败状态（由 Stage1Failed/Stage2Failed 设置）
          // 后端在阶段一/二失败时仍会推 done 事件（record.exception 非空），不能盲目推进到完成步
          const hasException = evt.record && evt.record.exception;
          if (!hasException) {
            setFlowBarStep(6);
            setTimeout(hideFlowBar, 5000);
          } else {
            // 异常时进度条保留失败状态更久（10s），让用户看清失败位置
            setTimeout(hideFlowBar, 10000);
          }
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
    currentAnalysisStream = null;
    chartUpdatePaused = false;
    // 分析结束后刷新一次完整数据
    loadBars();
    isAnalyzing = false;
    // 根据开关状态恢复按钮样式（waiting / idle）
    refreshAnalyzeButtonWaitingState();
  }
}

// ── 等待 K 线收盘倒计时（仅显示，不触发分析） ──────────────────────────────
// 用户勾选「等待收盘」复选框时调用，仅显示倒计时，不触发分析流程。
// 与 startWaitCloseCountdown 的区别：后者是点击分析按钮后等待收盘再触发分析。
async function startWaitingCountdownDisplay() {
  // 清理旧的显示定时器
  stopWaitingCountdownDisplay();

  // 还没有 next_close_ts（或已过期）时，先走一次 REST 拉取
  if (!sseNextCloseTs || sseNextCloseTs <= Date.now()) {
    await fetchAndUpdateNextCloseTs(true);
  }

  // 轮询流活跃：复用 sseStatusExpiryTimer，由 updateSSEStatusWithExpiry 更新按钮文本
  if (barsStreamPolling) {
    startSSEStatusExpiryTimer();
    // 立即更新一次按钮文本
    if (sseNextCloseTs > 0) {
      const remMs = sseNextCloseTs - Date.now();
      const rem = remMs <= 0 ? 0 : Math.ceil(remMs / 1000);
      if (rem > 0) updateWaitingButtonCountdown(rem);
    }
    return;
  }

  // 轮询流不活跃（实时关闭 / demo / 回看）：走独立 setInterval 更新按钮文本
  const computeRemaining = () => {
    if (!sseNextCloseTs || sseNextCloseTs <= 0) return -1;
    const remMs = sseNextCloseTs - Date.now();
    return remMs <= 0 ? 0 : Math.ceil(remMs / 1000);
  };

  let tickCount = 0;
  waitCloseDisplayTimer = setInterval(() => {
    const cb = $('#cb-wait-close');
    if (!cb || !cb.checked) {
      stopWaitingCountdownDisplay();
      return;
    }
    const remaining = computeRemaining();
    if (remaining < 0) {
      tickCount++;
      if (tickCount % 5 === 0) {
        fetchAndUpdateNextCloseTs(true);
      }
      return;
    }
    if (remaining <= 0) {
      // 倒计时归零，但不触发分析（只是显示），重置等待下一根
      fetchAndUpdateNextCloseTs(true);
      return;
    }
    updateWaitingButtonCountdown(remaining);
  }, 1000);
}

function stopWaitingCountdownDisplay() {
  if (waitCloseDisplayTimer) {
    clearInterval(waitCloseDisplayTimer);
    waitCloseDisplayTimer = null;
  }
  // 重置按钮文本为「等待收盘」
  const btn = $('#btn-analyze-toggle');
  if (btn && btn.dataset.state === 'waiting') {
    const textEl = btn.querySelector('.btn-analyze-text');
    if (textEl) textEl.textContent = '等待收盘';
  }
}

// ── 等待 K 线收盘倒计时（完整流程，触发分析） ──────────────────────────────
// 调 GET /api/bars/next-close 拿 seconds_remaining，setInterval 每秒递减更新
// #wait-close-countdown 文案「等待收盘：还剩 Ns」；归零后 clearInterval 并
// 返回 true 表示可以继续发起分析。用户中途取消勾选则返回 false。
async function startWaitCloseCountdown() {
  stopWaitCloseCountdown();

  // 还没有 next_close_ts（或已过期）时，先走一次 REST 拉取写入 sseNextCloseTs；
  // 失败也不退出，倒计时循环每秒会重新读取 sseNextCloseTs
  if (!sseNextCloseTs || sseNextCloseTs <= Date.now()) {
    await fetchAndUpdateNextCloseTs(true);
  }

  // 轮询流活跃：复用 sseStatusExpiryTimer（与状态栏共享同一 tick、同一 remaining），
  // 不创建独立 setInterval，彻底消除两个定时器不同步和算法不一致的问题。
  // 由 updateSSEStatusWithExpiry 在 remaining <= 0 时调用 resolver 触发分析。
  if (barsStreamPolling) {
    startSSEStatusExpiryTimer();
    // 立即更新一次按钮文本（不等下一个 tick，避免首次显示延迟）
    if (sseNextCloseTs > 0) {
      const remMs = sseNextCloseTs - Date.now();
      const rem = remMs <= 0 ? 0 : Math.ceil(remMs / 1000);
      if (rem > 0) updateWaitingButtonCountdown(rem);
    }
    return new Promise((resolve) => {
      waitCloseCountdownResolver = resolve;
    });
  }

  // 轮询流不活跃（实时关闭 / demo / 回看）：走独立 setInterval
  // updateSSEStatusWithExpiry 在此模式下不跑，需要自己维护倒计时
  const computeRemaining = () => {
    if (!sseNextCloseTs || sseNextCloseTs <= 0) return -1;
    const remMs = sseNextCloseTs - Date.now();
    return remMs <= 0 ? 0 : Math.ceil(remMs / 1000);
  };

  let tickCount = 0;
  let remaining = computeRemaining();
  if (remaining > 0) {
    updateWaitingButtonCountdown(remaining);
  }

  return await new Promise((resolve) => {
    waitCloseCountdownTimer = setInterval(() => {
      const cb = $('#cb-wait-close');
      if (cb && !cb.checked) {
        stopWaitCloseCountdown();
        resolve(false);
        return;
      }
      remaining = computeRemaining();
      // next_close_ts 未就绪：周期性拉取 REST 等待恢复，不归零触发
      if (remaining < 0) {
        tickCount++;
        if (tickCount % 5 === 0) {
          fetchAndUpdateNextCloseTs(true);
        }
        return;
      }
      if (remaining <= 0) {
        stopWaitCloseCountdown();
        // 倒计时归零，await loadBars 刷新 K线（含新 bar 的 seq/指标）后再触发分析
        loadBars()
          .then(() => {
            // 同上：统一用 continuous_gate.closedBarTs，防止持续分析重复触发
            const newClosedTs = window.PAContinuousGate
              ? window.PAContinuousGate.closedBarTs(lastBars)
              : 0;
            if (newClosedTs) {
              keepAnalysisLastClosedTs = newClosedTs;
            }
          })
          .catch((e) => console.error('startWaitCloseCountdown loadBars:', e))
          .finally(() => resolve(true));
        return;
      }
      updateWaitingButtonCountdown(remaining);
    }, 1000);
  });
}

function stopWaitCloseCountdown() {
  if (waitCloseCountdownTimer) {
    clearInterval(waitCloseCountdownTimer);
    waitCloseCountdownTimer = null;
  }
  // 先捕获 resolver 并调用 resolve(false)，再置 null
  // 避免外部调用时 startAnalysis 永久挂在 await startWaitCloseCountdown() 处
  if (waitCloseCountdownResolver) {
    const r = waitCloseCountdownResolver;
    waitCloseCountdownResolver = null;
    r(false);
  }
  // 重置等待按钮文本为「等待收盘」
  const btn = $('#btn-analyze-toggle');
  if (btn && btn.dataset.state === 'waiting') {
    const textEl = btn.querySelector('.btn-analyze-text');
    if (textEl) textEl.textContent = '等待收盘';
  }
}

// ── 增量分析（基于上次成功记录） ──────────────────────────────────────
// 复用 startAnalysis 的事件处理逻辑，仅切换 endpoint 为 /api/analyze/incremental/stream
async function startIncrementalAnalysis(continuousMode = false, triggerSource = 'user') {
  const cbWaitClose = $('#cb-wait-close');
  // 同 startAnalysis：持续分析由「刚收盘」触发时不再等一根
  const needWait = window.PAContinuousGate
    ? window.PAContinuousGate.shouldWaitForClose(triggerSource, cbWaitClose && cbWaitClose.checked)
    : !!(cbWaitClose && cbWaitClose.checked);
  if (needWait) {
    const started = await startWaitCloseCountdown();
    if (!started) return;
  }

  setAnalyzeButtonState('analyzing');
  isAnalyzing = true;
  chartUpdatePaused = true;
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
  const continuousParam = continuousMode ? '&continuous=true' : '';

  try {
    const { controller, source } = API.sse(`/api/analyze/incremental/stream?bar_count=${barCount}${continuousParam}`);
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
              applyCancelKeepAnalysisOnRetry('阶段一');
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
              setFlowBarFailed(2);
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
              applyCancelKeepAnalysisOnRetry('阶段二');
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
              setFlowBarFailed(4);
              break;
            case 'InsufficientData':
              stage = '⚠️ 数据不足，无法分析';
              if (evt.reason) stage += `：${evt.reason}`;
              setFlowBarFailed(2);
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
          // 主图 fitView 后同步副图时间轴（确保副图跟随主图逻辑范围）
          if (window._indicatorsAPI && window._indicatorsAPI.syncSubCharts) {
            window._indicatorsAPI.syncSubCharts();
          }
          enableChat();
          refreshIncrementalButtonState();
          // 下单机会提醒（Phase E Task 12）
          triggerOrderAlertIfNeeded(evt.record);
          // FlowBar：仅在无异常时标记完成步；异常时保留失败状态
          const hasException = evt.record && evt.record.exception;
          if (!hasException) {
            setFlowBarStep(6);
            setTimeout(hideFlowBar, 5000);
          } else {
            setTimeout(hideFlowBar, 10000);
          }
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
    currentAnalysisStream = null;
    chartUpdatePaused = false;
    // 分析结束后刷新一次完整数据
    loadBars();
    isAnalyzing = false;
    // 根据开关状态恢复按钮样式（waiting / idle）
    refreshAnalyzeButtonWaitingState();
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
  // record 为空 = 当前没有分析结果（模式切回实时 / 尚未分析）。
  // 此前本函数直接 record.stage2_decision，传 null 会抛 TypeError，
  // 于是「模式切回实时」只能绕过它 → 决策面板残留上一条记录的内容。
  if (!record) {
    const el0 = $('#decision-content');
    if (el0) el0.innerHTML = '<div class="muted-text">尚未进行交易分析</div>';
    return;
  }
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
// 95% 上下文告警只弹一次，避免流式期间反复刷屏
let _ctxWarn95Shown = false;

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
  // 缓存命中率：服务端对逐字相同的前缀按缓存价计费且 prefill 更快。
  // 不显示就看不出预热是否真的生效，所以直接摊在 token 明细里。
  const cached = usage.cached_prompt_tokens
    || (usage.prompt_tokens_details && usage.prompt_tokens_details.cached_tokens)
    || 0;
  const hitPct = promptTokens > 0 ? (cached / promptTokens) * 100 : 0;
  const cacheTxt = cached > 0
    ? `, 缓存命中 ${cached} (${hitPct.toFixed(0)}%)`
    : ', 缓存未命中';
  $('#token-progress-detail').textContent =
    `used=${totalTokens} / window=${contextWindow} (prompt=${promptTokens}, completion=${completionTokens}${cacheTxt})`;
  $('#token-progress-detail').classList.toggle('cache-good', hitPct >= 50);
  $('#token-progress-detail').classList.toggle('cache-bad', promptTokens > 0 && cached === 0);

  wrap.classList.remove('hidden', 'warn', 'danger');
  if (pct >= dangerPct) wrap.classList.add('danger');
  else if (pct >= warnPct) wrap.classList.add('warn');

  // 95% 上下文用量警告。
  // 原实现用 $('#token-progress-bar') 取元素，但页面上只有 class="token-progress-bar"
  // 且没有这个 id —— 于是 bar.classList.add('danger') 从未执行，警示色永远不生效。
  // 变红效果已由上面的 wrap.classList.add('danger') 生效（CSS:
  // .token-progress.danger .token-progress-fill），此处只需弹提示。
  // 另外提示必须去重：updateTokenProgress 在流式过程中会被调用多次，
  // 原实现每次 >=95% 都弹一次，会连续刷屏。
  if (pct >= 95 && !_ctxWarn95Shown) {
    _ctxWarn95Shown = true;
    showToast('上下文用量已超过 95%，建议开始新会话', 'warning');
  } else if (pct < 95) {
    _ctxWarn95Shown = false;
  }
}

function renderTokenUsage(usage) {
  // null = 清空。上一轮分析的 token 用量不是当前模式的产物，
  // 模式切回实时后继续显示会让人以为刚跑过一轮。
  const usageEl = $('#stream-usage');
  if (!usage) {
    if (usageEl) usageEl.textContent = '';
    return;
  }
  if (!usageEl) return;
  const cached = usage.cached_prompt_tokens
    || (usage.prompt_tokens_details && usage.prompt_tokens_details.cached_tokens) || 0;
  const pct = usage.prompt_tokens > 0 ? (100 * cached / usage.prompt_tokens).toFixed(0) : '0';
  $('#stream-usage').textContent =
    `Token: prompt=${usage.prompt_tokens || 0} completion=${usage.completion_tokens || 0}`
    + ` total=${usage.total_tokens || 0} | 缓存 ${cached} (${pct}%)`;
}

// ── 未来走势预期面板 ──────────────────────────────────────────────────
function renderFuturePanel(record) {
  const el = $('#future-content');
  if (!el) return;
  if (!record) {
    el.innerHTML = '<div class="future-empty">尚未进行交易分析</div>';
    return;
  }
  const d = record.stage2_decision || {};
  let html = '';

  // 下一根 K 线预期
  html += renderNextBarPrediction(d.next_bar_prediction);
  // 下一周期预期
  html += renderNextCyclePrediction(d.next_cycle_prediction);

  if (!html) {
    el.innerHTML = '<div class="future-empty">本轮分析未返回走势预测</div>';
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
  // record 为空 = 当前没有分析结果。补此前缺失的空态早退：
  // 直接传 null 会让本函数抛 TypeError，调用方只能绕过 → 面板残留旧内容。
  if (!record) {
    const el0 = $('#tree-content');
    if (el0) el0.innerHTML = '<div class="muted-text">尚未进行交易分析</div>';
    return;
  }
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
// 追问历史的回填状态。
//   chatHistoryKey —— 当前 #chat-messages 里画的是哪个线程。同键重复回填是
//                     幂等的，而画面上还多出本轮 live 追加的消息，不该被抹掉。
//   chatHistorySeq —— 请求序号。boot 与 enableChat() 可能同时发起回填，
//                     慢的那次响应回来会覆盖快的，故用序号丢弃过期响应。
let chatHistoryKey = null;
let chatHistorySeq = 0;

// 「加载中…」与「还没有追问记录」共用的提示行 class。
// 空 div 与「真的没聊过」在界面上长得一模一样 —— 用户分不清是加载失败、
// 正在加载、还是没有历史。把这两种态明确画出来，是 requirement 5 的底线。
const CHAT_NOTE_CLASS = 'chat-history-note';

function showChatNote(text) {
  const panel = $('#chat-messages');
  if (!panel) return;
  panel.innerHTML = `<div class="${CHAT_NOTE_CLASS} muted-text">${escapeHtml(text)}</div>`;
}

function enableChat() {
  const input = $('#chat-input');
  const sendBtn = $('#btn-chat-send');
  // 非实时模式下保持禁用：历史回看 / Demo 的追问会对着归档结论或合成数据提问，
  // 既无意义，又会把 Demo 数据写进记录。此前这里无条件放开，正好覆盖掉
  // setPanelsReadonly() 的禁用 —— 回看记录时追问框居然是可用的。
  const ro = isReadonly();
  if (input) input.disabled = ro;
  if (sendBtn) sendBtn.disabled = ro;
  renderChatContext();
  // 任何解锁追问的路径都要回填历史：刷新后锚点仍是同一条记录（服务端按
  // session_id + 从记录自推导的 record 段命中同一个桶），但前端内存里已经
  // 什么都没有了。
  backfillChatHistory();
}

/**
 * 从服务端取当前追问线程的历史并回填 #chat-messages。
 *
 * **只读不写**：回填的是历史展示，气泡里没有输入框、没有编辑入口 —— 用户能
 * 重发（#btn-chat-resend 只重发自己刚发的那一条），但改不了已经发生的对话。
 *
 * 三条纪律：
 * 1. **生成中绝不重画**。`done` 先推、落库在后（见 routes_chat 模块 docstring
 *    硬约束 2），那一瞬间库里还没有这一轮，重画会把它抹掉。
 * 2. **失败时不改任何状态**。宁可留着「加载中」，也不要画成「没有历史」——
 *    那是在用错误的空态掩盖一次网络抖动。
 * 3. **成功才重画**。回填以服务端给出的历史为准；同键重复回填直接跳过。
 */
async function backfillChatHistory() {
  const panel = $('#chat-messages');
  if (!panel) return;
  if (chatAbortController) return;          // 纪律 1
  const seq = ++chatHistorySeq;
  // 只有面板是空的时候才提示「加载中」：已经有内容（上一段对话 / 本轮 live
  // 消息）时把它盖成「加载中」反而是倒退。
  const wasEmpty = panel.children.length === 0;
  if (wasEmpty) showChatNote('正在加载追问历史…');
  let data;
  try {
    data = await API.get('/api/chat/turns?attach_kline_snapshot=true');
  } catch (e) {
    console.warn('chat history backfill failed:', e);   // 纪律 2
    if (wasEmpty) showChatNote('追问历史加载失败，可稍后重试');
    return;
  }
  if (seq !== chatHistorySeq) return;       // 有更新的请求在飞，丢弃这次
  renderChatHistory(data);
}

/** 把 GET /api/chat/turns 的返回画进 #chat-messages。 */
function renderChatHistory(data) {
  const panel = $('#chat-messages');
  if (!panel) return;
  const turns = Array.isArray(data?.turns) ? data.turns : [];
  const key = data?.thread_key || null;
  // 纪律 3：同一线程重画是幂等的，而画面上可能还叠着本轮 live 追加的消息 ——
  // 那正是用户要看的，重画等于把它清掉。换了线程才必须整块重画（留着上一段
  // 对话会让用户以为模型还记得它）。
  if (key && key === chatHistoryKey && panel.querySelector('.chat-msg')) {
    return;
  }
  chatHistoryKey = key;
  // 刷新后 lastRecord 是 null，#chat-context 会画「尚未进行交易分析」——
  // 与紧接着回填出来的历史当场矛盾。有锚点就用服务端给的锚点补上。
  if (!lastRecord && data?.source === 'db') {
    renderChatContext({ symbol: data.symbol, timeframe: data.timeframe });
  }
  panel.innerHTML = '';
  if (!turns.length) {
    showChatNote('还没有追问记录');
    return;
  }
  for (const t of turns) {
    if (t.user) appendChatMsg('user', t.user);
    if (t.cancelled) {
      // 取消的一轮：库里只有提问、没有回答。必须显式画出来，
      // 否则界面上凭空少一条 assistant，看起来像 UI 吞了消息。
      appendChatMsg('assistant', '[该轮追问被取消]');
      continue;
    }
    if (!t.assistant) {
      appendChatMsg('assistant', '[该轮追问没有回答]');
      continue;
    }
    const el = appendChatMsg('assistant', '');
    if (!el) continue;
    // 历史气泡绝不能带 id：#chat-content / #chat-reasoning 是**本轮**流式输出
    // 的落点，靠 `$('#chat-content')` 取第一个匹配项。回填出多个助手气泡后，
    // 下一轮追问的 token 会全部灌进最早那一条里（重复 id ⇒ 取首个）。
    el.querySelectorAll('[id]').forEach(n => n.removeAttribute('id'));
    const rEl = el.querySelector('.reasoning');
    if (rEl) rEl.textContent = t.reasoning || '';
    const cEl = el.querySelector('.bubble');
    if (cEl) cEl.textContent = t.assistant;
  }
  panel.scrollTop = panel.scrollHeight;
}

// 追问会话锚在哪一次分析上，必须让用户看得见 —— 否则切换品种/回看历史后
// 仍以为在追问上一份结论，实际早就换成了另一个锚点。
//
// anchor 是**可选**的降级锚点：刷新后 lastRecord 为 null，但服务端已经用
// 会话游标找回了锚点记录（回填响应里带着它的 symbol/timeframe）。此时若仍
// 画「尚未进行交易分析」，就会与下方刚回填出来的历史自相矛盾。
// 不传 anchor 时行为与改造前完全一致。
function renderChatContext(anchor) {
  const box = $('#chat-context');
  if (!box) return;
  const r = lastRecord;
  const a = anchor || null;
  if (!r && !a) {
    box.innerHTML = '<span class="chat-context-empty">尚未进行交易分析，完成后可在此追问</span>';
    return;
  }
  const sym = (r && (r.symbol || r.meta?.symbol)) || a?.symbol || $('#ds-symbol')?.value || '';
  const tf = (r && (r.timeframe || r.meta?.timeframe)) || a?.timeframe || $('#ds-timeframe')?.value || '';
  const ts = (r && (r.timestamp_local_iso || r.meta?.timestamp_local_iso)) || '';
  const ot = (r && (r.stage2_decision && (r.stage2_decision.order_type
        || r.stage2_decision.decision?.order_type))) || '';
  const time = ts ? new Date(ts).toLocaleString('zh-CN', { hour12: false }) : '';
  box.innerHTML = `<span class="chat-context-tag">锚定分析</span>`
    + `<span class="chat-context-item">${escapeHtml(sym)} · ${escapeHtml(tf)}</span>`
    + (ot ? `<span class="chat-context-item">${escapeHtml(ot)}</span>` : '')
    + (time ? `<span class="chat-context-time">${escapeHtml(time)}</span>` : '');
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
  // 显示重发按钮
  const resendBtn = $('#btn-chat-resend');
  if (resendBtn) resendBtn.style.display = '';

  sendBtn.textContent = '停止';
  sendBtn.classList.add('btn-danger');

  chatAbortController = new AbortController();
  chatReasoningText = '';
  chatContentText = '';

  // ⚠️ **不要在这里拼 record_id**。
  // 它此前派生自 lastRecord，而 lastRecord 每次页面加载重置为 null ⇒ 拼出来
  // 的 id 每次刷新都不同 ⇒ 服务端分桶键漂移 ⇒ FreeChatSession 在 30 分钟
  // TTL 内也永远命中不了（实测三连刷新得到三个互不相同的键）。
  // record 段改由服务端从锚点记录自推导（routes_chat._resolve_thread_key），
  // 前端只保留它真正知道的开关。
  const url = `/api/chat/stream?text=${encodeURIComponent(text)}&attach_kline_snapshot=true`;

  try {
    const { controller, source } = API.sse(url);
    // 将 API.sse 的 AbortController 与本地的合并（用户点停止时能中断）
    const origAbort = chatAbortController.abort.bind(chatAbortController);
    chatAbortController.abort = () => { origAbort(); controller.abort(); };

    for await (const evt of source) {
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
  // 追加到「追问」tab 的消息区（#tab-chat）。此前挂在 #tab-stream 里，
  // 追问作为主交互之一被埋在流式输出末尾，且需要滚动才能看到。
  const streamPanel = $('#chat-messages') || $('#tab-chat');
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
  const streamPanel = $('#chat-messages') || $('#tab-chat');
  if (!streamPanel) return;
  streamPanel.querySelectorAll('.chat-msg').forEach(el => el.remove());
  // 「正在加载…」/「还没有追问记录」也是回填画的，点一次「清空」必须一起清 ——
  // 否则消息没了却留着一句「还没有追问记录」。
  streamPanel.querySelectorAll('.chat-history-note').forEach(el => el.remove());
  // 画面的不再是「已回填的某线程」，下一次回填要重新画一遍。
  chatHistoryKey = null;
  const ctxEl = $('#chat-context');
  if (ctxEl) ctxEl.innerHTML = '';
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
// 默认加载当前 (exchange, symbol, timeframe) 的最近 50 条历史分析记录。
//
// 「全部品种」模式：历史记录是 **L2 用户级共享数据** —— 一个标签页分析出的
// 记录，另一个标签页也应当能看到（docs/SESSION_STORAGE_DESIGN.md §2.1）。
// 勾选后请求不带任何过滤条件，后端跨全部品种返回。
//
// 注意：过滤条件必须「三者齐全或三者皆空」。只传 symbol 无法用分区目录定位，
// 后端会退化成全扫描，比默认路径慢 —— 故部分过滤不作为 UI 选项暴露。
//
// ── 并发守卫（缺了它就是「勾了全部品种、接口 200、列表却是空的」）────────
// 本函数有 4 个触发点，且**都不是 await 的**（boot 行 528 明确不 await，
// popover 打开 / 刷新按钮 / 勾选框同样都是 fire-and-forget）。触发点之间可以
// 任意交错，而 `renderHistoryList` 是**就地覆写** `#history-list`：
//
//   T0  点「历史」        → GET /api/records?exchange=..&limit=50   （慢：扫全表载荷）
//   T1  勾「全部品种」    → GET /api/records?limit=50                （慢：返回全部）
//   T2  T1 的响应到达     → 渲染 25 条 ✅
//   T3  T0 的响应到达     → 渲染 [] → **「暂无历史记录」**，25 条被抹掉
//
// 实测（真实容器 + 真实 Chromium，游标 NVDA/5m/NASDAQ，该组合库里零行）：
// 勾选框处于勾选态、`?limit=50` 返回 200 共 25 条，列表最终却是
// 「暂无历史记录」—— 与「后端正常、前端空态」的现象完全吻合。
//
// 注意别把这个「回来的空列表」当成增量探针的锅：探针走的是 `limit=1`
// （app.js:refreshIncrementalButtonState）且**不渲染列表**。日志里那条
// 带过滤的 `limit=50` 请求是**本函数自己**在过滤分支发的。
//
// 修法：单调递增的序号，只有最新一次调用有资格渲染。早到的结果直接丢弃。
let _historyListSeq = 0;

async function loadHistoryList() {
  // 先占号：即使下面因为过滤条件不全而提前 return，也会让在途的旧请求作废，
  // 否则「最新的意图」会让「过期的响应」继续往 DOM 上写。
  const seq = ++_historyListSeq;
  const browseAll = !!$('#chk-history-all-symbols')?.checked;
  let url;
  if (browseAll) {
    url = '/api/records?limit=50';
  } else {
    const exchange = $('#ds-exchange').value || currentSettings?.general?.last_tradingview_exchange || '';
    const symbol = $('#ds-symbol').value || currentSettings?.general?.last_symbol || 'BTCUSDT';
    const timeframe = $('#ds-timeframe').value || currentSettings?.general?.last_timeframe || '1d';
    if (!exchange || !symbol || !timeframe) return;
    url = `/api/records?exchange=${encodeURIComponent(exchange)}&symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&limit=50`;
  }
  let data;
  try {
    data = await API.get(url);
  } catch (e) {
    if (seq !== _historyListSeq) return;   // 已有更新的请求在路上，别用它覆盖
    console.error('loadHistoryList:', e);
    // ⚠️ 读失败**不能**渲染成「暂无历史记录」：那是「确实没有记录」的说法。
    // 两者混同过一次真实误判 —— 接口 401/500 时用户看到的是「你没历史」，
    // 于是去反复重跑分析，而真正的问题是登录态或后端。
    renderHistoryError(e);
    return;
  }
  if (seq !== _historyListSeq) return;     // ← 守卫：过期响应就地丢弃
  renderHistoryList(data || [], { showSymbol: browseAll });
}

// 读失败的独立空态。与「暂无历史记录」视觉上区分开，避免用户误判成「没数据」。
function renderHistoryError(err) {
  const list = $('#history-list');
  if (!list) return;
  const msg = (err && err.message) ? String(err.message) : '未知错误';
  list.innerHTML = `<div class="history-empty muted-text">历史记录加载失败：${escapeHtml(msg.slice(0, 120))}</div>`;
}

// 渲染历史记录列表项到 popover
function renderHistoryList(records, opts = {}) {
  const showSymbol = !!opts.showSymbol;
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
    // 跨品种浏览时必须显示归属标的，否则一堆条目无法区分
    const symbolBadge = showSymbol && r.symbol
      ? `<span class="history-item-symbol">${escapeHtml(r.symbol)}·${escapeHtml(r.timeframe || '')}</span>`
      : '';
    // 交易所也要显示：同名同周期的标的在不同交易所下并不可分 ——
    // 页面上订阅的是 NASDAQ/NVDA/5m，列表里那条 NVDA·5m 却是 GATEIO 下的，
    // 于是「当前品类查不到、全部品种查得到」这个现象完全无法自查（写库取错
    // 游标是根因，但展示层若连交易所都不给，用户就只能来问）。
    // 用可读名而非裸代号；「当前品类」模式不加 —— 那里过滤已锁定
    // (exchange, symbol, timeframe) 三元组，再显示是冗余噪音。
    const exchangeName = showSymbol ? exchangeDisplayName(r.exchange) : '';
    const exchangeBadge = exchangeName
      ? `<span class="history-item-exchange">${escapeHtml(exchangeName)}</span>`
      : '';
    // 增量分析 / 持续分析标识
    const tags = [];
    if (r.incremental) tags.push('<span class="history-tag history-tag-incremental">增量</span>');
    if (r.continuous) tags.push('<span class="history-tag history-tag-continuous">持续</span>');
    const tagsHtml = tags.join('');
    return `<div class="history-item" data-record-id="${recordId}">
      ${symbolBadge}${exchangeBadge}
      <span class="history-item-time">${escapeHtml(time)}</span>
      ${closeBarTime ? `<span class="history-item-close-bar">📍 ${escapeHtml(closeBarTime)}</span>` : ''}
      ${tagsHtml}
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
    // 联动主图：切到该记录的品种/周期，绘制入场/止损/止盈
    await applyReplayChart(data);
    // 显示回看 badge
    showReplayBadge(data);
    // 关闭 popover
    $('#history-popover').classList.add('hidden');
    // 显示"返回实时"按钮
    // 切到决策 tab
    $$('.sidebar-tabs .tab').forEach(b => b.classList.remove('active'));
    document.querySelector('.sidebar-tabs .tab[data-tab="decision"]')?.classList.add('active');
    $$('.tab-panel').forEach(p => p.classList.remove('active'));
    $('#tab-decision').classList.add('active');
  } catch (e) {
    console.error('replayRecord:', e);
    showToast('加载历史记录失败', 'error');
  }
}

// ── 回看联动主图 ────────────────────────────────────────────────────────────
// 回看一条历史记录时，主图必须跟着切到**该记录**的品种/周期，并画出当时的
// 入场/止损/止盈横线。此前 replayRecord 只重渲染侧边栏，图表原封不动，
// 于是：图上是当前品种 K 线、面板里是历史记录的数字，两边完全对不上。
async function applyReplayChart(record) {
  const meta = record?.meta || {};
  const symbol = record?.symbol || meta.symbol;
  const timeframe = record?.timeframe || meta.timeframe;
  const exchange = meta.exchange || '';
  if (!symbol || !timeframe) return;

  try {
    // 暂存当前实时订阅（仅第一次回看时存，后续在同品种间切换不覆盖）
    if (!_liveSubBeforeReplay) {
      // 取 settings 的真实订阅状态；工具栏标签可能与之不一致。
      const g = currentSettings?.general || {};
      _liveSubBeforeReplay = {
        symbol: g.last_symbol || $('#ds-symbol')?.value || '',
        timeframe: g.last_timeframe || $('#ds-timeframe')?.value || '',
        exchange: g.last_tradingview_exchange || $('#ds-exchange')?.value || '',
      };
    }

    // 记录 instrument 后**无条件**重新订阅，不按工具栏标签做「无需切换」判断。
    // 标签与后端订阅可能不一致（例如别处直接调过 /api/subscribe），
    // 那种情况下按标签判断会跳过切换，导致图上仍是别的品种，
    // 而面板里显示历史记录的数字 —— 两边彻底对不上。
    if (window._indicatorsAPI) window._indicatorsAPI.clearAllData();
    const wasLive = $('#cb-live-refresh')?.checked;
    if (wasLive) stopSSEBarsStream();
    await API.post('/api/subscribe', {
      kind: 'tradingview', symbol, timeframe, exchange,
    }, { timeout: 15000 });
    await loadBars();
    // 工具栏同步，避免「图上是 ETHUSDT、工具栏写 NVDA」
    // ⚠ 必须**先于** startSSEBarsStream：轮询流的 startNextClosePolling 会同步
    // 发起一次 next-close 请求，读的是 #ds-symbol/#ds-exchange/#ds-timeframe；
    // 原来先启流后写 DOM，读到的是回看前的旧游标。
    const hid = $('#ds-symbol'); if (hid) hid.value = symbol;
    const shown = $('#ds-symbol-search'); if (shown) shown.value = symbol;
    const tf = $('#ds-timeframe'); if (tf) tf.value = timeframe;
    if (exchange) setCursorSelect($('#ds-exchange'), exchange);
    if (wasLive) startSSEBarsStream();

    // ── 先解析「分析当时」的锚点 bar ──────────────────────────────────
    // 必须在画方向箭头**之前**算好：箭头要落在当时那根 K 线上，
    // 而不是刚加载数据的最新一根（回看历史记录时两者相差几周）。
    //
    // 注意：老记录的分析时间可能已不在当前加载的数据范围内（例如回看
    // 8 月的 ETH，而当前只有 10 月的 K 线）。此时若硬对齐会被钳到序列
    // 边界、视窗退化成开头两三根超宽 K 线，所以范围外改为回退到近期窗口，
    // 并且**不画方向箭头** —— 画在一个不相关的 bar 上比不画更有害。
    let anchorSec = null;
    let total = 0;
    try {
      if (lastBars && lastBars.length) {
        const sorted = [...lastBars].sort((a, b) => a.ts_open - b.ts_open);
        total = sorted.length;
        const firstTs = sorted[0].ts_open / 1000;
        const lastTs = sorted[total - 1].ts_open / 1000;
        // 优先用服务端现算的权威锚点：旧记录的 last_close_bar_iso 里可能
        // 烙着差一根的值（当时 bars[0] 并非 forming bar），用 kline_data
        // 重新推导才能修正，且不必改写磁盘上的历史。
        const anchorMs = Number(record.anchor_bar_ts_ms || 0);
        const anchorTs = anchorMs > 0
          ? anchorMs / 1000
          : (record.last_close_bar_iso
              ? Date.parse(record.last_close_bar_iso) / 1000
              : lastTs);
        if (Number.isFinite(anchorTs) && anchorTs >= firstTs && anchorTs <= lastTs) {
          let nearest = 0, best = Infinity;
          sorted.forEach((b, i) => {
            const d = Math.abs(b.ts_open / 1000 - anchorTs);
            if (d < best) { best = d; nearest = i; }
          });
          // 用真实 bar 的 ts（marker 时间必须落在实际数据点上）
          anchorSec = Math.floor(sorted[nearest].ts_open / 1000);
          const half = Math.min(30, Math.floor(total / 3));
          const from = Math.max(0, nearest - half);
          const to = Math.min(total - 1, nearest + half);
          if (to > from) chart.timeScale().setVisibleLogicalRange({ from, to: to + 0.5 });
        } else {
          const win = Math.min(60, total);
          chart.timeScale().setVisibleLogicalRange({ from: total - win, to: total - 1 + 1.5 });
          anchorSec = null;   // 锚点不在数据范围内
        }
      }
    } catch (_) { /* 视窗调整失败不影响回看主体 */ }

    // 清掉上一次回看留下的横线，再画本次记录的
    clearOverlays(candleSeries);
    const overlay = record.decision_overlay || record.stage2_decision || {};
    setDecisionOverlays(candleSeries, overlay);
    setDirectionMarker(candleSeries, overlay, anchorSec);
    _renderTradeLegend(overlay);

    // 复盘模式不参与持续分析的自动触发判定，避免回看时误触发新一轮分析
    const cbKeep = $('#cb-keep-analysis');
    if (cbKeep && cbKeep.checked) {
      cbKeep.checked = false;
      cbKeep.dispatchEvent(new Event('change', { bubbles: true }));
    }
  } catch (err) {
    console.error('applyReplayChart:', err);
    showToast('历史记录图表联动失败，已保留当前视图', 'warning');
  }
}

// 显示回看 badge，并把记录时间填入 #replay-time
function showReplayBadge(record) {
  setDataMode('replay', record?.symbol
    ? `${record.symbol} · ${record.timeframe || ''}`.trim()
    : '');
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
  isReplaying = false;
}

// 删除历史记录：二次确认 → DELETE /api/records/{record_id} → 刷新列表
async function deleteRecord(recordId) {
  if (!recordId) return;
  // confirm() 在 TRAE 内置 webview 中同样会崩溃，改用 Toast 二次确认
  showToast('再次点击「删除」以确认删除该条历史记录', 'warning');
  if (!pendingDeleteId) {
    pendingDeleteId = recordId;
    return;
  }
  if (pendingDeleteId !== recordId) {
    pendingDeleteId = recordId;
    return;
  }
  pendingDeleteId = null;
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
// ── 经验库浏览 ──────────────────────────────────────────────────────────────
// 经验库此前没有写入方也没有浏览入口，这里是只读面板。
async function loadExperienceLibrary(opts) {
  const list = $('#exp-list');
  if (!list) return;
  // 交易对与周期**恒定取自当前 K 线订阅**，不提供手动选择：
  // 经验库的意义就是「我正在看的这个标的、这个周期上历史上怎么走」。
  const sym = ($('#ds-symbol')?.value || currentSettings?.general?.last_symbol || '').toUpperCase();
  const tf = ($('#ds-timeframe')?.value || currentSettings?.general?.last_timeframe || '');
  const showAll = !!(opts && opts.all);
  const cyc = showAll ? '' : ($('#exp-cycle')?.value || '');
  _expShowAll = showAll;

  const q = new URLSearchParams();
  if (!showAll) {
    if (sym) q.set('symbol', sym);
    if (tf) q.set('timeframe', tf);
  }
  if (cyc) q.set('cycle', cyc);

  // N 根 K 线的判定窗口长度，用于在条目上显示「已走 k/N 根」
  try {
    const n = Number(currentSettings?.prompt?.experience_verify_bars || 0);
    if (n > 0) window.__expVerifyBars = n;
  } catch (e) { /* 设置未加载时用默认值 */ }

  const scopeEl = $('#exp-scope');
  if (scopeEl) {
    scopeEl.textContent = showAll ? '全部品种与周期' : `${sym} · ${tf}`;
    scopeEl.title = showAll ? '当前浏览全库' : '经验库范围始终与当前 K 线一致';
  }

  list.innerHTML = '<div class="exp-empty">加载中…</div>';
  try {
    const d = await API.get(`/api/experience${q.toString() ? '?' + q.toString() : ''}`);
    const entries = d.entries || [];
    _fillExpSelect('#exp-cycle', d.cycle_options || [], '全部市场周期');

    const sc = d.status_counts || {};
    const pending = sc.pending || 0;
    const wins = entries.filter(e => e.result === 'win').length;
    const losses = entries.filter(e => e.result === 'loss').length;
    const decided = wins + losses;
    const parts = [`共 ${entries.length} 条`];
    if (decided) parts.push(`盈利 ${wins} / 亏损 ${losses}，胜率 ${Math.round(wins / decided * 100)}%`);
    if (pending) parts.push(`待验证 ${pending}`);
    if (sc.unresolved) parts.push(`未触及 ${sc.unresolved}`);
    if (cyc) parts.push(`市场周期 ${cyc}`);

    const summary = $('#exp-summary');
    if (summary) summary.textContent = parts.join(' · ');

    const vBtn = $('#btn-exp-verify');
    if (vBtn) {
      vBtn.disabled = pending === 0;
      vBtn.textContent = pending ? `验证 (${pending})` : '验证';
      vBtn.title = pending
        ? `按入场后的 N 根 K 线结算待验证记录（当前 N=${window.__expVerifyBars || 20}）`
        : '当前范围没有待验证记录';
    }
    const allBtn = $('#exp-show-all');
    if (allBtn) allBtn.hidden = !showAll || entries.length > 0;

    if (!entries.length) {
      list.innerHTML = showAll
        ? '<div class="exp-empty">经验库暂无任何条目。</div>'
        : `<div class="exp-empty">${escapeHtml(sym)} ${escapeHtml(tf)} 下暂无经验条目。<br>`
          + '出现下单信号后系统会立刻写入一条「待验证」，K 线走完后自动结算。</div>';
      return;
    }
    list.innerHTML = entries.map((e, i) => {
      const st = e.status || (e.result === 'win' ? 'win' : 'loss');
      const pnl = typeof e.pnl_pct === 'number' ? e.pnl_pct : null;
      const pats = (e.detected_patterns || []).slice(0, 3).join('、');
      const cycL = e.cycle_label || e.cycle_position || '';
      const dirL = e.direction_label || e.direction || '';
      const dir = e.is_long === false ? '空头' : '多头';
      const cls = st === 'pending' ? 'is-pending' : st === 'unresolved' ? 'is-unresolved'
                : st === 'win' ? 'is-success' : 'is-failure';
      const N = window.__expVerifyBars || 20;
      const prog = st === 'pending'
        ? `<span class="exp-tag">已走 ${e.bars_seen || 0}/${N} 根</span>` : '';
      return `<div class="exp-item ${cls}" data-exp-index="${i}">
        <div class="exp-head">
          <span class="exp-symbol">${escapeHtml(e.symbol || '—')}</span>
          <span class="exp-tag">${escapeHtml(e.timeframe || '')}</span>
          ${cycL ? `<span class="exp-tag" title="${escapeHtml(e.cycle_position || '')}">${escapeHtml(cycL)}</span>` : ''}
          ${dirL ? `<span class="exp-tag">${escapeHtml(dirL)}</span>` : ''}
          <span class="exp-tag ${st}">${escapeHtml(e.case_type_label || st)}</span>
          ${e.confidence != null ? `<span class="exp-tag">置信 ${e.confidence}</span>` : ''}
          ${prog}
          ${pnl != null ? `<span class="exp-pnl ${pnl >= 0 ? 'pos' : 'neg'}">${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}%</span>` : ''}
        </div>
        <div class="exp-summary">${escapeHtml(e.summary || '')}</div>
        <div class="exp-levels">入场 ${escapeHtml(String(e.entry_price ?? '—'))} · 止盈 ${escapeHtml(String(e.take_profit_price ?? '—'))} · 止损 ${escapeHtml(String(e.stop_loss_price ?? '—'))} · ${dir}</div>
        ${pats ? `<div class="exp-patterns">形态：${escapeHtml(pats)}</div>` : ''}
        <div class="exp-actions">
          <button class="exp-mini" data-review="${escapeHtml(e.filename || '')}"
                  title="让 AI 基于这条记录的完整上下文（当时的判断 + K 线窗口 + 实际结果）做事后复盘">复盘</button>
        </div>
        <div class="exp-review" data-review-for="${escapeHtml(e.filename || '')}"></div>
      </div>`;
    }).join('');

    // 复盘按钮：每条记录独立，不设全局按钮。事件委托 + stopPropagation，
    // 避免触发条目的图表回放。
    list.querySelectorAll('.exp-mini').forEach((btn) => {
      btn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        startExperienceReview(btn.dataset.review);
      });
    });

    // 点击条目 → 主图回放入场点 / 止盈 / 止损 / 判定区间
    list.querySelectorAll('.exp-item').forEach((el) => {
      el.addEventListener('click', () => {
        const rec = entries[Number(el.dataset.expIndex)];
        if (!rec) return;
        list.querySelectorAll('.exp-item.is-replaying').forEach(x => x.classList.remove('is-replaying'));
        el.classList.add('is-replaying');
        if (typeof window.setExperienceReplay === 'function') {
          window.setExperienceReplay(candleSeries, rec);
          const anchor = Number(rec.entry_ts_open_ms || 0);
          const win = Number(window.__PA_LAST_BAR_TIME__ || 0);
          if (anchor > 0 && win > 0) {
            const sec = anchor / 1000;
            const from = Math.max(0, Math.round((sec - win) / 3600) - 10);
            chart.timeScale().setVisibleLogicalRange({ from, to: from + 80 });
          }
        }
        showToast(`已在主图标出：${rec.symbol} ${rec.timeframe} 的入场 / 止盈 / 止损`, 'success');
      });
    });
  } catch (err) {
    list.innerHTML = `<div class="exp-empty">加载失败：${escapeHtml(String(err.message || err))}</div>`;
  }
}

// ── 数据源模式状态机 ────────────────────────────────────────────────────
// 三种模式：live（实时）/ replay（历史回看）/ demo（演示）。
// 此前 Demo 完全不在任何状态机里 —— 加载演示数据后界面与真实行情毫无区别，
// 用户会把它当成真实报价。三个按钮的 LED 与状态条由 setDataMode() 统一驱动，
// 不要在别处单独改 class。
let dataMode = 'live';

const DATA_MODE_META = {
  live:   { label: '实时行情', color: 'live' },
  replay: { label: '历史回看', color: 'replay' },
  demo:   { label: '演示数据', color: 'demo' },
};

function setDataMode(mode, subText) {
  const m = DATA_MODE_META[mode] ? mode : 'live';
  dataMode = m;
  const bar = $('#data-mode-bar');
  if (bar) bar.dataset.mode = m;
  const lbl = $('#dmb-label');
  if (lbl) lbl.textContent = DATA_MODE_META[m].label;
  const sub = $('#dmb-sub');
  if (sub) sub.textContent = subText || '';

  // 三个按钮：LED 点亮的那个 = 当前所处的模式
  $$('.act-btn').forEach(b => {
    const forMode = b.dataset.modeFor;
    b.classList.toggle('is-on', forMode === m);
    if (b.id === 'btn-live') {
      b.title = m === 'live' ? '当前已是实时行情' : `离开${DATA_MODE_META[m].label}，回到实时行情`;
      b.disabled = (m === 'live');
    }
    if (b.id === 'btn-demo') {
      b.title = m === 'demo' ? '当前正在看演示数据，再次点击重新加载' : '加载 Demo 演示数据（非真实行情）';
    }
    if (b.id === 'btn-history') {
      b.title = m === 'replay' ? '当前正在回看历史记录，点击可选择其它记录' : '查看历史分析记录（回看模式）';
    }
  });

  // 分析只在实时模式下有意义：回看的是已归档的分析结果、Demo 是合成数据，
  // 对着它们再跑一次分析既没意义又会污染记录。直接隐藏而不是 disabled ——
  // 灰按钮会让人反复去点它为什么点不动。
  setPanelsReadonly(m !== 'live');
  const isLive = (m === 'live');
  const analyzeBox = $('#btn-analyze-toggle');
  if (analyzeBox) analyzeBox.classList.toggle('hidden', !isLive);
  const analyzeRow = document.querySelector('.sidebar-header');
  if (analyzeRow) analyzeRow.classList.toggle('analyze-hidden', !isLive);
  $('#dmb-hint-analyze')?.classList.toggle('hidden', isLive);

  // 图表整体加一点色调提示，回看/Demo 时一眼能看出来
  try {
    document.body.dataset.dataMode = m;
  } catch (e) { /* 忽略 */ }

  window._dataMode = m;
  return m;
}

function currentDataMode() { return dataMode; }
// 非实时模式（历史回看 / Demo）下，侧边栏是**只读**的。
// 回看看到的是一份已归档的分析结果，Demo 是合成行情 —— 对着它们追问、
// 验证经验、重跑分析都没有意义，还会把 Demo 数据写进记录或经验库。
// 做法是禁用控件而不是弹提示：逐个 click 拦截迟早漏掉新增控件。
const READONLY_BLOCKED = [
  '#btn-chat-send', '#btn-chat-clear', '#btn-chat-resend',
  '#chat-input',
  '#btn-exp-verify', '#btn-exp-refresh',
  '#btn-history-refresh',
  '#cb-force-full', '#cb-wait-close', '#cb-keep-analysis',
  '#btn-tree-viz-play',
  '#s-alert-on-order-opportunity',
];

let _readOnly = false;

/**
 * 把所有「分析产出类」面板恢复到空态。
 *
 * 模式状态机（setDataMode）只负责 LED / 染色 / 只读，**不管面板内容** ——
 * 状态与内容是两套东西。回到实时时若不重置，预测 / 决策树 / 决策 / 流式
 * 四个面板会继续显示上一条记录的内容（2026-10-05 用户实机报告）。
 *
 * 统一调用各渲染函数的空态分支，而不是就地拼 innerHTML：
 * 空态文案与结构只有一处定义，改文案不会漏。
 */
function resetAnalysisPanels() {
  lastRecord = null;
  try { renderStreamFromRecord(null); } catch (e) { console.warn('reset stream:', e); }
  try { renderDecision(null); } catch (e) { console.warn('reset decision:', e); }
  try { renderFuturePanel(null); } catch (e) { console.warn('reset future:', e); }
  try { renderDecisionTree(null); } catch (e) { console.warn('reset tree:', e); }
  try { renderTreeViz(null); } catch (e) { console.warn('reset tree-viz:', e); }
  try { renderChatContext(); } catch (e) { console.warn('reset chat ctx:', e); }
  // Token 进度条 / 流程条 / 用量行同属上一轮分析的产物。
  // 注意：不要用 `updateFlowBarIdle?.()` —— 未声明的标识符即使加可选链
  // 仍会抛 ReferenceError（可选链只对「已声明为 undefined」生效）。
  try { updateTokenProgress(null); } catch (e) { /* 可选元素 */ }
  try { hideFlowBar(); } catch (e) { /* 可选元素 */ }
  try { renderTokenUsage(null); } catch (e) { /* 可选元素 */ }
}

function setPanelsReadonly(ro) {
  _readOnly = !!ro;
  // 用 attribute 而不是 classList：CSS 侧是 body[data-readonly] 属性选择器，
  // 加同名 class 选不中，降饱和/禁用态样式会完全不生效（曾如此）。
  if (_readOnly) document.body.setAttribute('data-readonly', '');
  else document.body.removeAttribute('data-readonly');

  READONLY_BLOCKED.forEach(sel => {
    $$(sel).forEach(el => {
      if (!el) return;
      if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'BUTTON') {
        el.disabled = _readOnly;
      }
    });
  });

  // 追问输入框的 placeholder 也要改，否则灰着还提示「可以问」
  const chat = $('#chat-input');
  if (chat) {
    chat.placeholder = _readOnly
      ? '回看 / 演示模式下不可追问 —— 切回「实时」后可提问'
      : (chat.dataset.ph || '针对本次分析提问…');
  }

  // 经验库条目上的复盘按钮是动态渲染的，用 CSS 兜住，不依赖枚举
  const tip = $('#readonly-hint');
  if (tip) tip.classList.toggle('hidden', !_readOnly);

  // 退出只读态后，各控件自己的禁用规则必须重新生效
  // （#btn-exp-verify 取决于 pending 数量，#chat-input 取决于是否解锁过）
  if (!_readOnly) {
    Promise.resolve()
      .then(() => refreshIncrementalButtonState())
      .then(() => { if (typeof loadExperienceLibrary === 'function') loadExperienceLibrary(); })
      .catch(() => {});
  }
}

function isReadonly() { return _readOnly; }

// 主图当前是否是演示数据。
// 不能只看 dataMode —— 加载 Demo 之后若用户又切了品种/周期，订阅变了但
// 图上仍是演示内容，此时仍需要一次强制重载才能回到真实 K 线。
let _demoBarsStale = false;
function isDemoDataActive() { return _demoBarsStale; }

let _incrementalReusable = false;
let _expSelectedSymbol = '';
let _expShowAll = false;
let _expReviewAbort = null;

// 单条经验的 LLM 复盘。刻意做成「就地展开」而不是跳到追问 tab：
// 复盘针对的是这一条记录，和当前图表/追问会话不是一回事，混在一起会
// 污染追问的上下文。
async function startExperienceReview(recordId) {
  if (!recordId) return;
  const box = document.querySelector(`.exp-review[data-review-for="${CSS.escape(recordId)}"]`);
  if (!box) return;
  if (_expReviewAbort) { try { _expReviewAbort.abort(); } catch (e) {} _expReviewAbort = null; }

  const running = box.querySelector('.exp-review-body');
  if (running) { box.innerHTML = ''; return; }   // 再点一次 = 收起

  box.innerHTML = '<div class="exp-review-head">复盘中…</div>'
    + '<div class="exp-review-body muted-text">正在读取档案并请求模型…</div>';
  box.scrollIntoView({ block: 'nearest' });

  const ctrl = new AbortController();
  _expReviewAbort = ctrl;
  let reasoning = '', content = '';
  try {
    const res = await fetch(`/api/experience/review/stream?record_id=${encodeURIComponent(recordId)}`,
      { signal: ctrl.signal });
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    const paint = () => {
      box.innerHTML = '<div class="exp-review-head">复盘 · 基于该记录的完整上下文</div>'
        + (reasoning ? `<details class="exp-review-reasoning"><summary>推理过程</summary>`
            + `<div class="exp-review-reasoning-body">${escapeHtml(reasoning)}</div></details>` : '')
        + `<div class="exp-review-body">${escapeHtml(content) || '<span class="muted-text">生成中…</span>'}</div>`;
    };
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      // sse_starlette 用 CRLF 分隔事件；不归一化就永远切不出完整事件。
      buf += dec.decode(value, { stream: true }).replace(/\r\n/g, '\n').replace(/\r/g, '\n');
      const parts = buf.split('\n\n');
      buf = parts.pop() || '';
      for (const part of parts) {
        let ev = '', data = '';
        for (const line of part.split('\n')) {
          if (line.startsWith('event:')) ev = line.slice(6).trim();
          else if (line.startsWith('data:')) data += line.slice(5).replace(/^ /, '');
        }
        if (ev === 'reasoning') reasoning += data;
        else if (ev === 'content') { content += data; paint(); }
        else if (ev === 'error') { box.innerHTML += `<div class="exp-review-error">复盘失败：${escapeHtml(data)}</div>`; }
      }
    }
    paint();
  } catch (e) {
    if (e.name !== 'AbortError') {
      box.innerHTML = `<div class="exp-review-error">复盘失败：${escapeHtml(String(e.message || e))}</div>`;
    }
  } finally {
    _expReviewAbort = null;
  }
}



function _fillExpSelect(sel, values, allLabel) {
  const el = document.querySelector(sel);
  if (!el) return;
  const cur = el.value;
  // 兼容两种形态：字符串数组，或 {value,label} 对象数组（枚举一律中英展示）
  const opts = (Array.isArray(values) ? values : []).map(v =>
    (v && typeof v === 'object') ? v : { value: v, label: String(v) });
  el.innerHTML = `<option value="">${escapeHtml(allLabel)}</option>`
    + opts.map(o => `<option value="${escapeHtml(o.value)}">${escapeHtml(o.label)}</option>`).join('');
  if (opts.some(o => o.value === cur)) el.value = cur;
}

async function initExperienceTab() {
  const sel = $('#exp-cycle');
  if (sel && !sel.dataset.filled) {
    try {
      const d = await API.get('/api/experience');
      const cycles = Object.keys(d.cycles || {});
      sel.innerHTML = '<option value="">全部周期</option>'
        + cycles.map(c => {
          const n = (d.cycles[c].success || 0) + (d.cycles[c].failure || 0);
          return `<option value="${escapeHtml(c)}">${escapeHtml(c)} (${n})</option>`;
        }).join('');
      sel.dataset.filled = '1';
      sel.addEventListener('change', loadExperienceLibrary);
    } catch (_) { /* 目录不存在时静默 */ }
  }
  const btn = $('#btn-exp-refresh');
  if (btn && !btn.dataset.bound) {
    btn.dataset.bound = '1';
    btn.addEventListener('click', loadExperienceLibrary);
  }
  const cycSel = $('#exp-cycle');
  if (cycSel && !cycSel.dataset.bound) {
    cycSel.dataset.bound = '1';
    cycSel.addEventListener('change', () => loadExperienceLibrary({ all: _expShowAll }));
  }
  const vBtn = $('#btn-exp-verify');
  if (vBtn && !vBtn.dataset.bound) {
    vBtn.dataset.bound = '1';
    vBtn.addEventListener('click', async () => {
      vBtn.disabled = true;
      const prev = vBtn.textContent;
      vBtn.textContent = '验证中…';
      try {
        // 走调度器的单飞守卫，避免与后台定时轮询并发读行情
        const r = await API.post('/api/experience/verify/once');
        const decided = (r.win || 0) + (r.loss || 0) + (r.unresolved || 0);
        if (decided) {
          showToast(`结算 ${decided} 条：盈利 ${r.win} / 亏损 ${r.loss} / 未触及 ${r.unresolved}`, 'success');
        } else if (r.pending) {
          showToast(`还有 ${r.pending} 条 K 线未走满，继续等待`, 'warning');
        } else if (r && r.skipped) {
          showToast('正在结算中，请稍后再试', 'warning');
        } else {
          showToast('暂无可结算的记录', 'warning');
        }
        await loadExperienceLibrary({ all: _expShowAll });
      } catch (e) {
        showToast('验证失败：' + (e.message || e), 'error');
        vBtn.textContent = prev;
        vBtn.disabled = false;
      }
    });
  }
  const allBtn = $('#exp-show-all');
  if (allBtn && !allBtn.dataset.bound) {
    allBtn.dataset.bound = '1';
    allBtn.addEventListener('click', () => {
      _expShowAll = !_expShowAll;
      loadExperienceLibrary({ all: _expShowAll });
    });
  }
  loadExperienceLibrary();
}

function showToast(message, type, opts) {
  // 会话失效后静默：登录页已经盖住主界面，此时在途请求的失败回调再弹一片
  // 错误提示，只会把登录框盖住、把「请重新登录」这条真正的信息挤掉。
  // 登录自身要提示的地方传 opts.force。
  if (typeof PAuth !== 'undefined' && PAuth.sessionDead && !(opts && opts.force)) return;
  // 复用已有 toast-container（HTML 中已定义，CSS 定位在右下角 z-index:9999）
  // type: 'success' | 'warning' | 'error' | undefined（默认中性灰）
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    container.className = 'toast-container';
    document.body.appendChild(container);
  }
  const toast = document.createElement('div');
  // 不使用 toast-card class，避免 CSS 动画导致 opacity 卡在 0
  // 直接用行内样式确保可见
  let bg = '#1e222d';
  let borderColor = '#363c4e';
  if (type === 'success') { bg = '#1e3a2e'; borderColor = '#26a69a'; }
  else if (type === 'warning') { bg = '#3a2e1e'; borderColor = '#ff9800'; }
  else if (type === 'error') { bg = '#3a1e1e'; borderColor = '#ef5350'; }
  toast.style.cssText = `background:${bg};color:#d1d4dc;border:1px solid ${borderColor};border-radius:6px;padding:10px 16px;font-size:13px;box-shadow:0 4px 12px rgba(0,0,0,0.5);max-width:300px;opacity:1;transform:none;pointer-events:auto;`;
  toast.textContent = message;
  container.appendChild(toast);
  // success/warning 显示 3 秒，error 显示 5 秒
  const ttl = type === 'error' ? 5000 : 3000;
  setTimeout(() => {
    if (toast.parentNode) toast.parentNode.removeChild(toast);
  }, ttl);
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
  const orderType = String(decision.order_type || '');
  const confidence = Number(decision.trade_confidence || 0);
  const threshold = Number(currentSettings?.general?.decision_confidence_threshold || 40);
  // 阶段二输出的是中文订单类型（限价单/突破单/市价单），与后端
  // ORDER_OPPORTUNITY_TYPES 一致。此前这里比对英文 ['limit','market','stop']，
  // 永远匹配不上 —— 浏览器端的下单提醒（toast/beep/通知）从未触发过。
  if (!ORDER_OPPORTUNITY_TYPES.includes(orderType)) return;
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
  const streamTab = $('#tab-stream');
  if (!streamTab) return;
  // 无记录时也要清理：此前直接 return，回看留下的 banner 与流式内容会一直留着
  if (!record) {
    streamTab.querySelectorAll('.replay-banner').forEach(el => el.remove());
    const flow = $('#flow-bar');
    if (flow) flow.classList.add('hidden');
    const body = $('#stream-body') || $('#stream-content');
    if (body) body.innerHTML = '';
    renderChatContext();
    return;
  }

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
