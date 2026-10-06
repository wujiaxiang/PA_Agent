/* 轮询 / 数据源模式的回归测试。
 *
 * 运行：node tests/js/test_live_refresh_modes.test.js
 *
 * ## 覆盖的两个症状
 *
 *   A. 「定时刷新 K 线（5 秒轮询）把正在进行的分析打断」
 *      —— 实测后拆成两条独立契约分别锁死：
 *        A1 分析期间**不得**写主图（chartUpdatePaused 生效，仍继续取数）
 *        A2 轮询**不得**碰分析请求（不 abort、不重置阶段、不覆盖流式 DOM）
 *      两条都靠「发了什么请求 / 调了哪个函数」判定，不看 class。
 *
 *   B. 「切到 Demo 模式后轮询仍在跑，Demo 数据被真实轮询覆盖」
 *      —— 非实时模式（Demo / 回看）下推进 5000ms×N，一个 `/api/bars`
 *        都不许发；回到实时必须**无条件**重载 K 线并恢复轮询。
 *
 * ## 为什么必须真跑 app.js / api.js
 *
 * 病根横跨三处：`setDataMode()` 写 dataMode → `startLiveRefresh()` 起定时器
 * → `refreshBarsOnly()` 写主图与 lastBars。任何只切一个函数的写法都会把
 * 「模式状态没人回头管定时器」这一段拿掉，于是测试永远绿。故本文件用最小
 * DOM 桩 + `vm` 真的把 api.js 与 app.js 跑起来。
 *
 * ## DOM 骨架从真实 index.html 扫 id
 *
 * 手抄清单一漂移，`bindEvents()` 就会因为某个元素「不存在」中途抛，后面
 * 所有绑定（包括「Demo」「返回实时」两个按钮）都没接上，而测试还在绿。
 *
 * ## 假定时器
 *
 * setInterval 被换成手动触发器：推进虚拟时钟 5000ms×N 才放行回调，
 * 于是「5 秒轮询」是真的在数 5 秒，而不是靠 sleep 碰运气。
 */
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..', '..');
const APP_JS = path.join(ROOT, 'web', 'static', 'js', 'app.js');
const API_JS = path.join(ROOT, 'web', 'static', 'js', 'api.js');
const INDEX_HTML = path.join(ROOT, 'web', 'static', 'index.html');

/* ══ 1. 最小 DOM 桩 ═══════════════════════════════════════════════════ */

let BODY = null;                      // parentNode 兜底目标（showToast 会用）
const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const escapeText = (s) => String(s).replace(/[&<>"']/g, (c) => ESCAPES[c]);

function makeStyle() {
  const target = {};
  return new Proxy(target, {
    set: (o, k, v) => { o[k] = v; return true; },
    get: (o, k) => (k in o ? o[k] : ''),
    deleteProperty: (o, k) => { delete o[k]; return true; },
  });
}

class ClassList {
  constructor() { this._s = new Set(); }
  add(...c) { c.forEach((x) => x && this._s.add(x)); }
  remove(...c) { c.forEach((x) => this._s.delete(x)); }
  contains(c) { return this._s.has(c); }
  toggle(c, force) {
    const want = force === undefined ? !this._s.has(c) : !!force;
    want ? this._s.add(c) : this._s.delete(c);
    return want;
  }
  get value() { return [...this._s].join(' '); }
}

class El {
  constructor(tag, id) {
    this.tagName = (tag || 'div').toUpperCase();
    this.id = id || '';
    this.children = [];
    this.attrs = {};
    this.style = makeStyle();
    this.dataset = {};
    this._html = '';
    this._text = null;
    this.classList = new ClassList();
    this._listeners = {};
    this._parent = null;
    this.value = '';
    this.title = '';
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.open = true;
  }
  set className(v) { this.classList._s = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return this.classList.value; }
  set innerHTML(v) { this._html = String(v); this._text = null; }
  get innerHTML() { return this._text !== null ? escapeText(this._text) : this._html; }
  set textContent(v) { this._text = v === null ? '' : String(v); this._html = ''; }
  get textContent() { return this._text !== null ? this._text : String(this._html).replace(/<[^>]*>/g, ''); }
  addEventListener(t, f) { (this._listeners[t] = this._listeners[t] || []).push(f); }
  removeEventListener(t, f) {
    const arr = this._listeners[t] || [];
    const i = arr.indexOf(f);
    if (i >= 0) arr.splice(i, 1);
  }
  /** 触发一次事件。两处按 HTML 规范来，桩偷懒会让用例测到另一条路径：
   *   1) checkbox 的 checked 翻转发生在 click 的 activation 阶段；
   *   2) **disabled 的表单控件不派发 click**（也不翻转 checked）。
   *      setPanelsReadonly() 正是靠 disabled 把非实时模式下的控件封掉，
   *      桩若无视它，「Demo 下点不动『实时』开关」这条就会永远测不到。 */
  fire(t) {
    if (t === 'click' && this.disabled) return null;
    if (t === 'click' && this.tagName === 'INPUT' && this.attrs.type === 'checkbox') {
      this.checked = !this.checked;
    }
    const ev = { type: t, target: this, bubbles: true, stopPropagation() {}, preventDefault() {} };
    (this._listeners[t] || []).slice().forEach((f) => f.call(this, ev));
    return ev;
  }
  appendChild(c) { this.children.push(c); c._parent = this; return c; }
  insertBefore(node) { this.children.push(node); node._parent = this; return node; }
  removeChild(c) {
    const i = this.children.indexOf(c);
    if (i >= 0) this.children.splice(i, 1);
    c._parent = null;
    return c;
  }
  remove() { if (this._parent) this._parent.removeChild(this); }
  contains(n) { return n === this || this.children.some((c) => c.contains && c.contains(n)); }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  getAttribute(k) { return this.attrs[k]; }
  setAttribute(k, v) { this.attrs[k] = v; }
  removeAttribute(k) { delete this.attrs[k]; }
  dispatchEvent() { return true; }
  focus() {} blur() {} scrollIntoView() {}
  click() { this.fire('click'); }
  closest() { return null; }
  matches() { return false; }
  getBoundingClientRect() { return { width: 800, height: 400, left: 0, top: 0 }; }
  get parentNode() { return this._parent || BODY; }
  set parentNode(v) { this._parent = v; }
  get firstChild() { return this.children[0] || null; }
}

class Doc extends El {
  constructor() {
    super('document');
    this.byId = {};
    this.all = [];
    this.listeners = {};
    this.hidden = false;
    this.title = '';
  }
  createElement(t) { return new El(t); }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  querySelector(sel) {
    if (typeof sel !== 'string') return null;
    if (sel.startsWith('#')) return this.byId[sel.slice(1)] || null;
    if (sel.startsWith('.')) { const c = sel.slice(1); return this.all.find((e) => String(e.attrs.class || '').split(/\s+/).includes(c)) || null; }
    const m = sel.match(/^([a-zA-Z]+)\.([\w-]+)$/);
    if (m) return this.all.find((e) => e.tagName === m[1].toUpperCase() && String(e.attrs.class || '').split(/\s+/).includes(m[2])) || null;
    return this.all.find((e) => e.tagName === sel.toUpperCase()) || null;
  }
  querySelectorAll(sel) {
    if (typeof sel !== 'string') return [];
    if (sel.startsWith('#')) {
      const root = this.byId[sel.slice(1)];
      if (!root) return [];
      const out = [];
      (function walk(n) { out.push(n); n.children.forEach(walk); })(root);
      return out;
    }
    if (sel.startsWith('.')) { const c = sel.slice(1); return this.all.filter((e) => String(e.attrs.class || '').split(/\s+/).includes(c)); }
    const m = sel.match(/^([a-zA-Z]+)\.([\w-]+)$/);
    if (m) return this.all.filter((e) => e.tagName === m[1].toUpperCase() && String(e.attrs.class || '').split(/\s+/).includes(m[2]));
    return this.all.filter((e) => e.tagName === sel.toUpperCase());
  }
  getElementById(id) { return this.byId[id] || null; }
}

/* ══ 2. 样本数据 ═══════════════════════════════════════════════════════ */

/** 真实 /api/bars 响应形状（routes_data.py::get_bars，bars[0] 为最新一根） */
const REAL_BARS = [
  { seq: 0, ts_open: 1759999300000, open: 1.5, high: 2.0, low: 1.0, close: 1.8, volume: 10, closed: false },
  { seq: 1, ts_open: 1759999000000, open: 1.0, high: 2.0, low: 0.5, close: 1.5, volume: 12, closed: true },
];
/** 演示数据的价格量级刻意差三个数量级：图上一眼能分清被不被刷掉 */
const DEMO_BARS = [
  { ts_open: 1759999300000, open: 60500, high: 61500, low: 60000, close: 60500, volume: 3, closed: false },
  { ts_open: 1759999000000, open: 60000, high: 61000, low: 59000, close: 60250, volume: 4, closed: true },
];
const DEMO_SAMPLE = {
  sample: { symbol: 'BTCUSDT' },
  symbol: 'BTCUSDT',
  timeframe: '15m',
  kline_data: DEMO_BARS,
  decision_overlay: { entry_price: 60500, stop_loss: 59000, take_profit: 65000 },
};
/** 回看记录：价格量级同样与 REAL_BARS 差很远，便于断言主图内容 */
const REPLAY_RECORD = {
  record_id: 'NASDAQ/NVDA/5m/2026-10-04_17-10-13_001_a1b2c3',
  symbol: 'NVDA', timeframe: '5m', exchange: 'NASDAQ',
  meta: { timestamp: '2026-10-04T17:10:13.001+00:00', exchange: 'NASDAQ' },
  anchor_bar_ts_ms: 1759975200000,
  stage2_decision: { entry_price: 100, stop_loss: 95, take_profit: 110 },
  decision_overlay: { entry_price: 100, stop_loss: 95, take_profit: 110 },
  kline_data: [
    { ts_open: 1759975500000, open: 100.5, high: 102, low: 100, close: 101.5, closed: false },
    { ts_open: 1759975200000, open: 100, high: 101, low: 99, close: 100.5, closed: true },
  ],
};

function makeToken(claims) {
  const b64 = (o) => Buffer.from(JSON.stringify(o), 'utf8').toString('base64')
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  return `v1.${b64(claims)}.${b64({ sig: true })}`;
}

/* ══ 3. 页面装配 ═══════════════════════════════════════════════════════ */

/** chart.js 提供的绘图原语。app.js 引用但不定义，桩掉即可 —— 本文件只关心
 *  「谁被调了、什么时候被调」，不关心画出来的像素。 */
const CHART_STUBS = [
  'setBars', 'setSeqMarkers', 'clearOverlays', 'fitView', 'setDecisionOverlays',
  'setDirectionMarker', 'setSupportResistance', '_renderTradeLegend', 'clearTradeLegend',
  'clearExperienceLegend', 'clearExperienceReplay', 'setExperienceReplay',
  '_renderExperienceLegend', 'setDisplayTimezone', '_clearPriceLines', '_applyMarkers',
  'createChart', '_hasBarAt',
];

function buildPage(opts) {
  const o = opts || {};
  const doc = new Doc();

  // DOM 骨架直接从**真实 index.html** 扫出来
  const html = fs.readFileSync(INDEX_HTML, 'utf8');
  for (const m of html.matchAll(/<([a-zA-Z][\w-]*)\b([^>]*)>/g)) {
    const [, tag, attrs] = m;
    const idM = attrs.match(/\bid="([^"]+)"/);
    const clsM = attrs.match(/\bclass="([^"]+)"/);
    const typeM = attrs.match(/\btype="([^"]+)"/);
    const e = new El(tag, idM ? idM[1] : '');
    if (clsM) e.attrs.class = clsM[1];
    if (typeM) e.attrs.type = typeM[1];
    if (idM) doc.byId[idM[1]] = e;
    doc.all.push(e);
  }
  const must = ['btn-demo', 'btn-live', 'cb-live-refresh', 'ds-symbol', 'ds-exchange',
    'ds-timeframe', 'ds-bar-count', 'ds-symbol-search', 'stage-badge', 'stage1-content',
    'stage2-content', 'future-content', 'decision-content', 'tree-content',
    'live-refresh-status', 'data-mode-bar', 'dmb-label', 'dmb-sub', 'tab-decision',
    'tab-stream', 'btn-analyze-toggle', 'btn-apply-subscribe', 'chat-input'];
  for (const id of must) {
    assert.ok(doc.byId[id], `index.html 里找不到 #${id} —— DOM 桩与真实骨架漂移`);
  }

  doc.body = new El('body');
  BODY = doc.body;

  const setVal = (id, v) => { if (doc.byId[id]) doc.byId[id].value = v; };
  setVal('ds-exchange', 'NASDAQ');
  setVal('ds-symbol', 'NVDA');
  setVal('ds-timeframe', '5m');
  setVal('ds-bar-count', '100');
  if (o.liveRefreshChecked) doc.byId['cb-live-refresh'].checked = true;

  const nowSec = Math.floor(Date.now() / 1000);
  const store = { pa_token: makeToken({ sub: 'admin', iat: nowSec - 60, exp: nowSec + 3600 }) };
  const storage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  };

  /* ── 假定时器：手动推进虚拟时钟 ── */
  let vnow = 1760000000000;
  let seq = 0;
  const timers = new Map();
  const arm = (fn, ms, once) => {
    const id = ++seq;
    timers.set(id, { fn, ms: Number(ms) || 0, next: vnow + (Number(ms) || 0), once: !!once });
    return id;
  };
  const setIntervalFake = (fn, ms) => arm(fn, ms, false);
  const clearFake = (id) => { timers.delete(id); };
  const setTimeoutFake = (fn, ms) => arm(fn, ms, true);
  function advance(ms) {
    const target = vnow + ms;
    for (let guard = 0; guard < 100000; guard++) {
      let pick = null;
      for (const [id, t] of timers) {
        if (t.next > target) continue;
        if (!pick || t.next < pick.t.next || (t.next === pick.t.next && id < pick.id)) pick = { id, t };
      }
      if (!pick) break;
      vnow = pick.t.next;
      if (pick.t.once) timers.delete(pick.id); else pick.t.next = vnow + pick.t.ms;
      try { pick.t.fn(); } catch (e) { console.error('  [定时器回调抛异常]', pick.t.ms, e && e.message); }
    }
    vnow = target;
  }

  const RealDate = Date;
  class FakeDate extends RealDate {
    constructor(...a) { if (a.length === 0) { super(vnow); } else { super(...a); } }
    static now() { return vnow; }
  }

  /* ── 网络：记录**每一个**请求，默认立即回 200 ──
   * hold 是一个正则：命中的 URL **不立即返回**，由用例自己 entry.release()，
   * 用来复刻「请求在途时用户切了模式」这条竞态。 */
  const calls = [];
  const hold = o.hold || null;
  function payloadFor(u) {
    if (u.startsWith('/api/bars/next-close')) return { next_close_ts: vnow + 120000, market_closed: false };
    if (u.startsWith('/api/bars?')) return { symbol: 'NVDA', timeframe: '5m', exchange: 'NASDAQ', bars: REAL_BARS };
    if (u.startsWith('/api/demo/sample')) return DEMO_SAMPLE;
    if (/^\/api\/records\/[^/]/.test(u)) return REPLAY_RECORD;
    if (u.startsWith('/api/settings')) return { general: { last_symbol: 'NVDA', last_timeframe: '5m', last_tradingview_exchange: 'NASDAQ' } };
    if (u.startsWith('/api/health')) return { status: 'ok' };
    return {};
  }
  function makeResponse(body) {
    return {
      ok: true, status: 200,
      json: async () => body,
      text: async () => JSON.stringify(body),
      headers: { get: () => null },
    };
  }
  function doFetch(url) {
    const u = String(url);
    const body = payloadFor(u);
    if (hold && hold.test(u)) {
      // 挂起：等用例显式 release()，模拟「响应还在路上」
      const entry = { url: u, _released: false, release: null };
      calls.push(entry);
      entry.release = () => {
        entry._released = true;
        entry.resolve_(makeResponse(body));
      };
      return new Promise((resolve) => { entry.resolve_ = resolve; });
    }
    calls.push({ url: u });
    return Promise.resolve(makeResponse(body));
  }

  const sandbox = {
    console: {
      log() {}, info() {},
      warn(...a) { (globalThis.__warns = globalThis.__warns || []).push(a.map(String).join(' ')); },
      error(...a) { (globalThis.__errs = globalThis.__errs || []).push(a.map((x) => (x && x.stack) ? String(x.stack).split('\n')[0] : String(x)).join(' ')); },
    },
    setTimeout: setTimeoutFake,
    clearTimeout: clearFake,
    setInterval: setIntervalFake,
    clearInterval: clearFake,
    document: doc,
    fetch: doFetch,
    window: { addEventListener() {}, _indicatorsAPI: null },
    localStorage: storage, sessionStorage: storage,
    AbortController, TextDecoder, URL, URLSearchParams, Buffer,
    location: { href: 'http://localhost/', reload() {} },
    navigator: { userAgent: 'node' },
    alert() {}, confirm: () => true, prompt: () => '1',
    requestAnimationFrame: () => 0,
    Event: function () {}, CustomEvent: function () {},
    Date: FakeDate,
  };
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(API_JS, 'utf8'), sandbox, { filename: 'api.js' });

  for (const name of CHART_STUBS) sandbox[name] = () => {};
  sandbox.chart = { timeScale: () => ({ fitContent() {}, setVisibleLogicalRange() {}, scrollToPosition() {} }), applyOptions() {} };
  sandbox.candleSeries = {};

  vm.runInContext(fs.readFileSync(APP_JS, 'utf8'), sandbox, { filename: 'app.js' });

  /* applyBarsToChart 探针：app.js 内部**所有**调用都经过这个 global 绑定，
   * 所以「写主图」这件事被完整地拦下来了。 */
  const chartWrites = [];
  const realApply = vm.runInContext('applyBarsToChart', sandbox);
  sandbox.applyBarsToChart = function (bars) {
    const list = Array.isArray(bars) ? bars : [];
    chartWrites.push(list.slice());
    return realApply.apply(null, arguments);
  };

  const ev = (code) => vm.runInContext(code, sandbox);
  return {
    doc, sandbox, calls, chartWrites, advance, ev,
    api: vm.runInContext('API', sandbox),
    get now() { return vnow; },
    /* ── 断言辅助：只看「发了什么请求 / 写了什么图」 ── */
    barsRequests() { return calls.filter((c) => c.url.startsWith('/api/bars?')).length; },
    nextCloseRequests() { return calls.filter((c) => c.url.startsWith('/api/bars/next-close')).length; },
    urls() { return calls.map((c) => c.url); },
    /** 在 document 上派发事件（如 visibilitychange） */
    fireDoc(type) {
      doc.hidden = false;
      (doc.listeners[type] || []).slice().forEach((f) => f({ type }));
    },
    /** 主图当前显示的数据（最后一次 applyBarsToChart 写进去的内容） */
    chartBars() { return chartWrites.length ? chartWrites[chartWrites.length - 1] : []; },
    chartLatestClose() {
      const b = this.chartBars();
      return b.length ? b[0].close : null;
    },
    reset() { calls.length = 0; chartWrites.length = 0; },
  };
}

const flush = async (n = 4) => { for (let i = 0; i < n; i++) await new Promise((r) => setImmediate(r)); };

/** 打开「实时」开关 → 轮询应当开始工作。走的是真实的 change 事件路径。 */
async function startLive(page) {
  page.ev('bindEvents()');
  await flush();
  page.ev("setDataMode('live')");
  await flush();
  const cb = page.doc.byId['cb-live-refresh'];
  if (!cb.checked) {
    cb.checked = true;          // 真人勾选时的 pre-click activation
    cb.fire('change');
  }
  await flush();
  return page;
}

/* ══ 4. 用例 ═══════════════════════════════════════════════════════════ */

const cases = [];
function test(name, fn) { cases.push({ name, fn }); }

/* ── A1 实时模式：轮询确实在跑，并且确实写主图 ──────────────────────
 * 这是「非实时模式必须停」的**对照**：没有它，停了轮询也算全绿。 */
test('A1 实时模式下推进 5000ms×3 → 真的请求 /api/bars 且真的写主图', async () => {
  const p = await startLive(buildPage({}));
  p.reset();
  p.advance(5000 * 3);
  await flush();
  assert.ok(p.barsRequests() >= 3, `15s 内至少应发 3 次 /api/bars，实际 ${p.barsRequests()} 次：${p.urls()}`);
  assert.ok(p.chartWrites.length >= 3, `应至少写 3 次主图，实际 ${p.chartWrites.length} 次`);
  assert.strictEqual(p.chartLatestClose(), REAL_BARS[0].close,
    '主图上的 K 线必须是 /api/bars 返回的那一批');
});

/* ── A2 分析中：仍取数、但一次都不许写主图 ────────────────────────────
 * 这条锁的是 AGENTS.md「refreshBarsOnly 必须尊重 chartUpdatePaused：
 * 仍更新 lastBars，只跳过 applyBarsToChart」。 */
test('A2 分析中推进 15s → chartUpdatePaused=true、零次 applyBarsToChart、lastBars 仍在更新', async () => {
  const p = await startLive(buildPage({}));
  p.advance(5000);            // 先让 lastBars 有数据
  await flush();

  let release = null;
  const gate = new Promise((r) => { release = r; });
  p.api.sse = () => ({
    controller: { signal: { aborted: false }, abort() { throw new Error('abort 不该被调用'); } },
    source: (async function* () {
      yield { type: 'orchestrator_event', event: 'Stage1Started' };
      await gate;
      yield { type: 'orchestrator_event', event: 'done', record: { stage2_decision: {}, meta: {} } };
    })(),
  });

  p.reset();
  const running = p.sandbox.startAnalysis();
  running.catch(() => {});
  await flush();
  assert.strictEqual(p.ev('chartUpdatePaused'), true, '分析一开始就必须置位 chartUpdatePaused');

  p.advance(5000 * 3);
  await flush();
  assert.ok(p.barsRequests() >= 3,
    `分析期间仍应继续取数（持续分析的收盘判定依赖最新 lastBars），实际 ${p.barsRequests()} 次`);
  assert.strictEqual(p.chartWrites.length, 0,
    `分析期间一次都不许写主图，实际写了 ${p.chartWrites.length} 次 —— 图表每 5s 跳一次`);
  assert.strictEqual(p.ev('(lastBars||[]).length'), REAL_BARS.length, 'lastBars 必须继续更新');
  assert.strictEqual(p.ev('isAnalyzing'), true, '分析不应被轮询打断');

  // 分析结束：finally 必须复位，并补一次全量刷新把图表续上
  release();
  await flush(); await flush(); await flush();
  assert.strictEqual(p.ev('chartUpdatePaused'), false, '分析结束必须复位 chartUpdatePaused');
  assert.strictEqual(p.chartWrites.length, 1,
    `分析结束应恰好补一次 loadBars 写图，实际 ${p.chartWrites.length} 次`);
  assert.strictEqual(p.chartLatestClose(), REAL_BARS[0].close, '补刷后主图必须是最新 K 线');
});

/* ── A3 分析中：轮询不得覆盖流式渲染的 DOM，也不得中止分析请求 ───────── */
test('A3 分析中推进 15s → 流式面板内容逐字不变、分析请求未被 abort', async () => {
  const p = await startLive(buildPage({}));
  p.advance(5000);
  await flush();

  const aborted = [];
  let release = null;
  const gate = new Promise((r) => { release = r; });
  p.api.sse = () => ({
    controller: { signal: { aborted: false }, abort() { aborted.push(1); } },
    source: (async function* () {
      yield { type: 'orchestrator_event', event: 'Stage1Started' };
      await gate;
      yield { type: 'orchestrator_event', event: 'done', record: { stage2_decision: {}, meta: {} } };
    })(),
  });

  const running = p.sandbox.startAnalysis();
  running.catch(() => {});
  await flush();
  const stageBadge = p.doc.byId['stage-badge'].textContent;
  const future = p.doc.byId['future-content'].innerHTML;
  assert.ok(stageBadge.includes('阶段一'), `分析应已推进到阶段一，实际「${stageBadge}」`);

  p.advance(5000 * 4);
  await flush();
  assert.strictEqual(p.doc.byId['stage-badge'].textContent, stageBadge, '状态徽标被轮询覆盖了');
  assert.strictEqual(p.doc.byId['future-content'].innerHTML, future, '预测面板被轮询覆盖了');
  assert.strictEqual(aborted.length, 0, '轮询不得 abort 分析流');
  assert.strictEqual(p.ev('currentAnalysisStream !== null'), true, '分析流控制器还在，中途被置空说明被打断');

  release();
  await flush(); await flush();
});

/* ── A4 置位/复位必须成对：前置步骤抛异常不得把 chartUpdatePaused 卡死 ──
 * 两个分析入口都是「先 chartUpdatePaused = true，再清面板（**无保护**的
 * DOM 写），再进 try/finally」。置位写在 try 之外时，上面任何一句抛异常都
 * 会跳过 finally —— 图表从此再也不刷新，且 isAnalyzing 卡住会把后续所有
 * 分析一起挡掉。这里用一个被真实破坏的 DOM（少一个面板容器）把那条窄路
 * 走一遍，断言的是「图表还在刷新」这个**结果**，不是标志位本身。 */
test('A4 分析前置步骤抛异常 → 图表不得永久停止刷新', async () => {
  const p = await startLive(buildPage({}));
  p.advance(5000);
  await flush();
  const writesBefore = p.chartWrites.length;

  // 破坏前置步骤依赖的 DOM（模拟容器被改版/移除）
  delete p.doc.byId['future-content'];
  await p.sandbox.startAnalysis().catch(() => {});

  assert.strictEqual(p.ev('chartUpdatePaused'), false,
    '前置步骤抛异常后 chartUpdatePaused 被永久留在 true —— 图表再也不刷新');

  p.advance(5000 * 2);
  await flush();
  assert.ok(p.chartWrites.length > writesBefore,
    '出异常之后轮询应当照常刷新主图（实际一次都没写）');
  assert.strictEqual(p.chartLatestClose(), REAL_BARS[0].close, '主图应被真实 K 线刷新');
});

/* ── B1 Demo：进入 Demo 后一个真实 K 线请求都不许发 ──────────────────
 * 本 bug 的现场：点 Demo → setDataMode('demo') → 轮询照跑 →
 * 5 秒后主图上已经是真实行情。 */
test('B1 点 Demo 后推进 10s → 零个 /api/bars 请求、零次写图，演示数据留在主图上', async () => {
  const p = await startLive(buildPage({}));
  p.advance(5000);
  await flush();

  p.reset();
  p.doc.byId['btn-demo'].fire('click');
  await flush(); await flush();
  assert.strictEqual(p.ev('currentDataMode()'), 'demo', '点 Demo 必须进入 demo 模式');
  assert.strictEqual(p.chartLatestClose(), DEMO_BARS[0].close, 'Demo 数据应已画到主图上');

  const writesAfterDemo = p.chartWrites.length;
  p.calls.length = 0;
  p.advance(5000 * 2);
  await flush();
  assert.strictEqual(p.barsRequests(), 0,
    `Demo 模式下轮询仍在跑（${p.barsRequests()} 次 /api/bars）—— Demo 数据 5 秒就被真实 K 线刷掉了：${p.urls()}`);
  assert.strictEqual(p.nextCloseRequests(), 0,
    `Demo 模式下 next-close 轮询也应停止，实际 ${p.nextCloseRequests()} 次`);
  assert.strictEqual(p.chartWrites.length, writesAfterDemo, 'Demo 模式下不允许再写主图');
  assert.strictEqual(p.chartLatestClose(), DEMO_BARS[0].close, '主图必须仍然是演示数据');
});

/* ── B2 模式迁移链：live → demo → live 必须连成一条链 ────────────────
 * AGENTS.md：「把多个操作串成一条状态迁移链逐段验证」。拆成两个独立步骤
 * 会漏掉「回实时时轮询没恢复」这一段。 */
test('B2 迁移链 live → demo → live：退出 Demo 必须无条件重载 K 线并恢复轮询', async () => {
  const p = await startLive(buildPage({}));
  p.advance(5000);
  await flush();

  // ── live → demo
  p.doc.byId['btn-demo'].fire('click');
  await flush(); await flush();
  p.advance(5000 * 2);
  await flush();
  assert.strictEqual(p.chartLatestClose(), DEMO_BARS[0].close, 'Demo 期间主图被真实轮询刷掉了');

  // ── demo → live（走真实的「返回实时」按钮）
  p.reset();
  p.doc.byId['btn-live'].fire('click');
  await flush(); await flush(); await flush();
  assert.strictEqual(p.ev('currentDataMode()'), 'live', '应回到实时模式');
  assert.ok(p.barsRequests() >= 1,
    `从 Demo 返回必须**无条件**重载 K 线（Demo 覆盖主图却不改订阅），实际 ${p.barsRequests()} 次`);
  assert.strictEqual(p.chartLatestClose(), REAL_BARS[0].close,
    '返回实时后主图必须换成真实 K 线，而不是留着演示数据');

  // ── live 之后轮询必须恢复（否则这一段没人管，用户会以为「实时死了」）
  p.reset();
  p.advance(5000 * 2);
  await flush();
  assert.ok(p.barsRequests() >= 2,
    `回到实时后轮询未恢复（${p.barsRequests()} 次），或用户关着的「实时」开关被无视：${p.urls()}`);
});

/* ── B3 回看：replayRecord() 结束时会把轮询重新开起来 ────────────────
 * 真实路径（applyReplayChart 末尾 `if (wasLive) startSSEBarsStream()`，
 * 而 setDataMode('replay') 在它之后才跑），所以回看同样被真实 K 线覆盖。 */
test('B3 回看一条历史记录后推进 10s → 零个 /api/bars 请求', async () => {
  const p = await startLive(buildPage({}));
  p.advance(5000);
  await flush();

  await p.sandbox.replayRecord(REPLAY_RECORD.record_id);
  await flush(); await flush(); await flush();
  assert.strictEqual(p.ev('currentDataMode()'), 'replay', '应进入回看模式');

  p.reset();
  p.advance(5000 * 2);
  await flush();
  assert.strictEqual(p.barsRequests(), 0,
    `回看模式下轮询仍在跑（${p.barsRequests()} 次）—— 回放画面被真实 K 线覆盖：${p.urls()}`);
  assert.strictEqual(p.chartWrites.length, 0, '回看模式下不允许再写主图');
});

/* ── B4 在途请求：请求发出后模式才切走，这一包响应必须作废 ────────────
 * 只在 refreshBarsOnly 里加**入口**闸门是不够的：请求已经在路上时用户点了
 * Demo，响应落地那一刻照样会把主图刷成真实 K 线。所以 await 之后必须再判一次。 */
test('B4 轮询请求在途时切到 Demo → 该响应落地后不得写主图', async () => {
  const p = buildPage({ liveRefreshChecked: true, hold: /^\/api\/bars\?/ });
  p.ev('bindEvents()');
  await flush();
  p.ev("setDataMode('live')");
  await flush();
  p.ev('startSSEBarsStream()');
  await flush();
  p.calls.length = 0;

  p.advance(5000);                       // 轮询请求起飞，此刻**不**返回
  await flush();
  const held = p.calls.filter((c) => typeof c.release === 'function');
  assert.ok(held.length >= 1, '应有一个在途的 /api/bars 请求');

  // 用户在响应回来之前切到 Demo（Demo 自己的请求不走 hold，正常返回）
  p.doc.byId['btn-demo'].fire('click');
  await flush(); await flush();
  assert.strictEqual(p.ev('currentDataMode()'), 'demo', '应进入 Demo 模式');
  assert.strictEqual(p.chartLatestClose(), DEMO_BARS[0].close, 'Demo 数据应已画到主图上');

  // 在途的真实响应此刻才落地
  held.forEach((c) => c.release());
  await flush(); await flush(); await flush();
  assert.strictEqual(p.chartLatestClose(), DEMO_BARS[0].close,
    `切到 Demo 后在途的真实响应仍写了主图：${p.chartWrites.map((b) => b[0].close)}`);
  assert.strictEqual(p.chartWrites.filter((b) => b[0].close === REAL_BARS[0].close).length, 0,
    'Demo 模式下主图上不许出现真实 K 线');
});

/* ── B5 非实时模式下，别的入口也不许把轮询拉起来 ────────────────────
 * 闸门不能只加在「定时器起表」那一处。app.js 里还有两条直接调
 * refreshBarsOnly() / startSSEBarsStream() 的旁路：
 *   · 标签页从后台恢复（visibilitychange → refreshBarsOnly() 直调）
 *   · 任何地方再调一次 startSSEBarsStream()（它内部会拉 next-close）
 * 它们在非实时模式下都必须同样发不出请求。注意 next-close 也要断言：
 * refreshBarsOnly 的闸门管不到它，只有 startLiveRefresh 的闸门 /
 * setDataMode 的停表能。 */
test('B5 非实时模式下经 visibilitychange / startSSEBarsStream → 一个请求都不许发', async () => {
  const p = await startLive(buildPage({}));
  p.advance(5000);
  await flush();
  p.doc.byId['btn-demo'].fire('click');
  await flush(); await flush();

  // 1) 标签页从后台恢复 → visibilitychange 里是**直调** refreshBarsOnly()
  p.calls.length = 0;
  p.fireDoc('visibilitychange');
  await flush();
  p.advance(5000 * 2);
  await flush();
  assert.strictEqual(p.barsRequests(), 0,
    `标签页恢复时 refreshBarsOnly() 直调把真实 K 线拉了回来（${p.barsRequests()} 次）：${p.urls()}`);
  assert.strictEqual(p.nextCloseRequests(), 0,
    `next-close 轮询也被拉起来了（${p.nextCloseRequests()} 次）：${p.urls()}`);

  // 2) 直接再调一次 startSSEBarsStream（visibilitychange 里就是它）
  p.calls.length = 0;
  p.ev('startSSEBarsStream()');
  await flush();
  p.advance(5000 * 2);
  await flush();
  assert.strictEqual(p.barsRequests(), 0,
    `Demo 下 startSSEBarsStream() 仍在发 /api/bars（${p.barsRequests()} 次）：${p.urls()}`);
  assert.strictEqual(p.nextCloseRequests(), 0,
    `Demo 下 startSSEBarsStream() 仍在发 next-close（${p.nextCloseRequests()} 次）：${p.urls()}`);
  assert.strictEqual(p.chartLatestClose(), DEMO_BARS[0].close, '主图必须仍然是演示数据');
});

/* ── B6 非实时模式下「实时」开关必须点不动 ──────────────────────────
 * 它是数据流开关，只在实时模式下有意义。留着可点，用户一点就会起一条
 * 幽灵轮询流：状态条开始写「● 实时轮询」，而实际上一根 K 线都不更新。
 * 断言的是**用户操作的结果**（点了没反应、没有任何请求发出）。 */
test('B6 Demo 下点「实时」勾选框完全不起作用', async () => {
  const p = await startLive(buildPage({}));
  p.advance(5000);
  await flush();
  p.doc.byId['btn-demo'].fire('click');
  await flush(); await flush();

  const cb = p.doc.byId['cb-live-refresh'];
  assert.strictEqual(cb.checked, true, '前置条件：进入 Demo 前「实时」是开着的');
  cb.checked = false;
  p.calls.length = 0;
  cb.fire('click');                      // 用户点了
  await flush();
  p.advance(5000 * 2);
  await flush();
  assert.strictEqual(cb.checked, false, 'Demo 下「实时」开关点不动 —— 它已被只读态封掉');
  assert.strictEqual(p.barsRequests(), 0, `点了之后不该有任何轮询请求：${p.urls()}`);
  assert.strictEqual(p.nextCloseRequests(), 0, `点了之后不该有任何轮询请求：${p.urls()}`);
  assert.strictEqual(p.chartLatestClose(), DEMO_BARS[0].close, '主图必须仍然是演示数据');
});

/* ── C 资产版本号：改了 app.js 必须递增（缓存兜底） ─────────────────── */
test('C index.html 给 app.js 带 ?v= 版本号且已递增', async () => {
  const html = fs.readFileSync(INDEX_HTML, 'utf8');
  const m = html.match(/<script src="\/js\/app\.js\?v=(\d+)"><\/script>/);
  assert.ok(m, 'index.html 必须给 app.js 加 ?v= 版本号');
  assert.ok(Number(m[1]) >= 72, `app.js 改过了，?v= 必须递增到 >=72，实际 ${m[1]}`);
});

/* ══ 5. 跑起来 ═══════════════════════════════════════════════════════ */

(async function run() {
  let failed = 0;
  for (const c of cases) {
    try {
      await c.fn();
      console.log('✅ %s', c.name);
    } catch (e) {
      failed++;
      console.error('❌ %s\n   %s', c.name, e && e.message);
    }
  }
  if (failed) {
    console.error('\n%d/%d 条失败', failed, cases.length);
    process.exit(1);
  }
  console.log('\n全部 %d 条通过', cases.length);
})().catch((e) => { console.error(e); process.exit(1); });

setTimeout(() => {
  console.error('\n❌ 用例未在预期时间内结束（多半是某个 await 永远挂着）');
  process.exit(1);
}, 30000).unref?.();