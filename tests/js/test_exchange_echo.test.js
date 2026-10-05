/* 刷新页面后交易所/周期下拉回显。
 *
 * 运行：node tests/js/test_exchange_echo.test.js
 *
 * ## 要守护的病
 *
 * 会话游标（服务端 `/api/settings` 的 `general.last_*`）是唯一真源。刷新后
 * 工具栏必须回显它，否则用户以为在看 A 标的、实际按 B 标的取数，且全程无报错。
 *
 * ## 为什么 DOM 桩必须**真的**实现 <select>（本文件的核心）
 *
 * `#ds-exchange` / `#ds-timeframe` 是 `<select>`，选项由 `loadExchanges()` /
 * `loadTimeframes()` 注入。HTML 规范里有两条会把 bug 藏起来的语义：
 *
 *   1. `select.value = <不存在的值>` → 选中集清空、`.value` **静默变 ''**、
 *      `selectedIndex === -1`。用 `<input>` 假冒 select 的桩里 `.value` 是
 *      随便一个字符串，赋值永远「成功」—— 于是「硬设 .value」这个**错误修法**
 *      在弱桩下测不出来。
 *   2. 解析出的选项里若**没有任何** `selected`，单选 select 会自动选中**首项**
 *      （规范里的 "ask for a reset"）。所以「回显失败」不是停在空，而是悄悄
 *      落到第一个选项（GATEIO）或那个 `value=""` 的「自动（探测）」。
 *
 * 桩里少实现任何一条，本文件就退化成「测了个不存在于现实的世界」。
 *
 * ## 为什么跑真实 app.js 而不是把回显逻辑抄一份
 *
 * 要守护的正是「`dataset.current` ↔ `.value` ↔ `currentSettings` 三者是否
 * 保持一致」这条**连线**。把 `setCursorSelect` 抄成纯函数测，抄的人会把
 * 「选项何时就位」也一起编出来。故此处用最小 DOM 桩 + `vm` 跑真实 api.js /
 * app.js，fetch 桩按 URL 分发、喂真实形状的响应。
 *
 * 选项样本取自实测（`GET /api/tv/exchanges` 20 条、`GET /api/timeframes` 13 条），
 * 保留那个 `id: ''` 的「自动（探测）」—— 它正是静默回落最容易落进去的坑。
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

/* ══ 1. 最小 DOM 桩 ═══════════════════════════════════════════════════════
 * 与 history 系列的桩相比，本文件只多做一件事：把 <select>/<option> 的语义
 * 补齐。其余部分保持同样风格，便于对照。 */

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const escapeText = (s) => String(s).replace(/[&<>"']/g, (c) => ESCAPES[c]);

class ClassList {
  constructor() { this._s = new Set(); }
  add(...c) { c.forEach((x) => this._s.add(x)); }
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
    this.style = {};
    this.dataset = {};
    this._html = '';
    this._text = null;
    this.classList = new ClassList();
    this._listeners = {};
    this.title = '';
    this.hidden = false;
    this.disabled = false;
    // 非 select 元素的普通 .value（<input>…）
    this._v = '';
    // <select> 的选中态
    this._options = null;   // null = 尚未解析出任何 <option>（= 空壳 select）
    this._sel = -1;
  }
  get isSelect() { return this.tagName === 'SELECT'; }

  set className(v) { this.classList._s = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return this.classList.value; }

  set innerHTML(v) {
    this._html = String(v);
    this._text = null;
    // 与浏览器一致：重写 innerHTML 会丢掉原有子节点
    this.children = [];
    if (this.isSelect) this._parseOptions();
    this._parseChildren();
  }
  get innerHTML() { return this._text !== null ? escapeText(this._text) : this._html; }
  set textContent(v) { this._text = v === null ? '' : String(v); this._html = ''; }
  get textContent() { return this._text !== null ? this._text : this._html.replace(/<[^>]*>/g, ''); }

  /* ── 真实 <select>/<option> 语义 ────────────────────────────────────── */

  /** 解析 `loadExchanges()` 写进来的 `<option value="X" selected>label</option>`。 */
  _parseOptions() {
    this._options = [];
    this._sel = -1;
    const re = /<option\b([^>]*)>([\s\S]*?)<\/option>/gi;
    let m;
    let i = 0;
    while ((m = re.exec(this._html)) !== null) {
      const attrs = m[1];
      const label = m[2];
      const vm2 = attrs.match(/\bvalue\s*=\s*"([^"]*)"/i);
      this._options.push({
        value: vm2 ? vm2[1] : escapeText(label),
        selected: /\bselected\b/i.test(attrs),
      });
      i += 1;
    }
    // 规范 "ask for a reset"：单选 select 没有任何 selected 时，首项自动选中。
    // 少这一条，「回显失败」在桩里会表现为空，而真机上会悄悄落到 GATEIO。
    if (this._sel === -1 && this._options.some((o) => o.selected)) {
      this._sel = this._options.findIndex((o) => o.selected);
    } else if (this._sel === -1 && this._options.length) {
      this._sel = 0;
    }
  }

  get options() {
    return (this._options || []).map((o, i) => ({ value: o.value, selected: i === this._sel }));
  }
  get selectedIndex() { return this._sel; }
  /** 已就位的选项值集合；空壳 select 返回空数组（= 选项还没加载） */
  optionValues() { return (this._options || []).map((o) => o.value); }

  get value() {
    if (!this.isSelect) return this._v;
    // 无选中项 ⇒ ''（**不是**首项）
    if (this._sel < 0 || !this._options || this._sel >= this._options.length) return '';
    return this._options[this._sel].value;
  }
  set value(v) {
    if (!this.isSelect) { this._v = String(v); return; }
    const s = String(v);
    const i = (this._options || []).findIndex((o) => o.value === s);
    // ⚠ 关键语义：值不存在 ⇒ 无选中项 ⇒ 后续读 .value 得到 ''。
    // **不**回落首项、也**不**记住写进去的值。
    this._sel = i;
  }

  addEventListener(t, f) { (this._listeners[t] = this._listeners[t] || []).push(f); }
  removeEventListener() {}
  fire(t) {
    const ev = { type: t, target: this, stopPropagation() {}, preventDefault() {} };
    (this._listeners[t] || []).slice().forEach((f) => f.call(this, ev));
    return ev;
  }
  appendChild(c) { this.children.push(c); return c; }
  contains(n) { return n === this || this.children.some((c) => c.contains && c.contains(n)); }

  /** 解析成对标签，供 querySelector('.cls') 找到（showSwitchError 依赖它）。 */
  _parseChildren() {
    const re = /<([a-zA-Z][\w-]*)\b([^>]*)>([\s\S]*?)<\/\1>/g;
    let m;
    while ((m = re.exec(this._html)) !== null) {
      const [, tag, attrs] = m;
      const clsM = attrs.match(/\bclass="([^"]+)"/);
      const idM = attrs.match(/\bid="([^"]+)"/);
      const e = new El(tag, idM ? idM[1] : '');
      if (clsM) e.attrs.class = clsM[1];
      e._text = m[3];
      this.children.push(e);
    }
  }
  _walk(fn) {
    let hit = null;
    (function walk(n) {
      for (const c of n.children) {
        if (hit) return;
        if (fn(c)) hit = c; else walk(c);
      }
    })(this);
    return hit;
  }
  querySelector(sel) {
    if (typeof sel === 'string' && sel.startsWith('.')) {
      const c = sel.slice(1);
      return this._walk((e) => String(e.attrs.class || '').split(/\s+/).includes(c));
    }
    return null;
  }
  querySelectorAll(sel) {
    if (typeof sel === 'string' && sel.startsWith('.')) {
      const c = sel.slice(1);
      const out = [];
      (function walk(n) {
        n.children.forEach((ch) => {
          if (String(ch.attrs.class || '').split(/\s+/).includes(c)) out.push(ch);
          else walk(ch);
        });
      })(this);
      return out;
    }
    return [];
  }
  getAttribute(k) { return this.attrs[k]; }
  setAttribute(k, v) { this.attrs[k] = v; }
  removeAttribute(k) { delete this.attrs[k]; }
  hasAttribute(k) { return k in this.attrs; }
  focus() {} blur() {} click() { this.fire('click'); }
  closest() { return null; }
  matches() { return false; }
  remove() {}
}

class Doc extends El {
  constructor() {
    super('document');
    this.byId = {};
    this.all = [];
    this.listeners = {};
    this.title = '';
  }
  createElement(t) { return new El(t); }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  /** index.html 里不存在的东西必须查不到 —— 桩不替实现补元素。 */
  querySelector(sel) {
    if (typeof sel !== 'string') return null;
    if (sel.startsWith('#')) return this.byId[sel.slice(1)] || null;
    if (sel.startsWith('.')) {
      const c = sel.slice(1);
      return this.all.find((e) => String(e.attrs.class || '').split(/\s+/).includes(c)) || null;
    }
    return this.all.find((e) => e.tagName === sel.toUpperCase()) || null;
  }
  querySelectorAll(sel) {
    if (typeof sel !== 'string') return [];
    if (sel.startsWith('#')) { const r = this.byId[sel.slice(1)]; return r ? [r] : []; }
    if (sel.startsWith('.')) {
      const c = sel.slice(1);
      return this.all.filter((e) => String(e.attrs.class || '').split(/\s+/).includes(c));
    }
    return this.all.filter((e) => e.tagName === sel.toUpperCase());
  }
  getElementById(id) { return this.byId[id] || null; }
}

/* ══ 2. 样本（形状逐字段取自实测响应） ═══════════════════════════════════ */

/** 实测 `GET /api/tv/exchanges` ��� 20 条，含一条 id 为空的「自动（探测）」。 */
const EXCHANGES_PAYLOAD = [
  { id: 'GATEIO', label: 'Gate.io' },
  { id: 'BINANCE', label: 'Binance' },
  { id: 'BYBIT', label: 'Bybit' },
  { id: 'OKX', label: 'OKX' },
  { id: 'OANDA', label: 'OANDA' },
  { id: 'SSE', label: '上交所' },
  { id: 'SZSE', label: '深交所' },
  { id: 'HKEX', label: '港交所' },
  { id: 'SP', label: 'S&P' },
  { id: 'NYSE', label: '纽交所' },
  { id: 'NASDAQ', label: '纳斯达克' },
  { id: 'CBOT', label: 'CBOT' },
  { id: '', label: '自动（探测）' },
];
/** 实测 `GET /api/timeframes`。 */
const TIMEFRAMES_PAYLOAD = ['1m', '3m', '5m', '15m', '30m', '45m', '1h', '2h', '3h', '4h', '1d', '1w', '1M'];

/** 生产库实况：会话游标 NVDA / 5m / NASDAQ。 */
const CURSOR = { last_symbol: 'NVDA', last_tradingview_exchange: 'NASDAQ', last_timeframe: '5m' };

/* ══ 3. 装载真实 app.js ═══════════════════════════════════════════════════ */

function makeToken(claims) {
  const b64 = (o) => Buffer.from(JSON.stringify(o), 'utf8').toString('base64')
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  return `v1.${b64(claims)}.${b64({ sig: true })}`;
}

/**
 * @param {object} opts
 *   settings      —— /api/settings 响应体（默认用 CURSOR）
 *   settingsFail  —— /api/settings 返回 500
 *   exchanges     —— /api/tv/exchanges 响应体
 *   exchangesFail —— /api/tv/exchanges 返回 500
 *   timeframes    —— /api/timeframes 响应体
 */
function buildPage(opts) {
  const o = opts || {};
  const doc = new Doc();

  // DOM 骨架从**真实 index.html** 扫出来，而不是手抄一份
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

  // 前置条件：index.html 里这两个控件确实是空壳 <select>，一个 <option> 都没有。
  // 这决定了「选项未就位时不能写 .value」这条策略的前提是真的。
  assert.strictEqual(doc.byId['ds-exchange'].tagName, 'SELECT', 'index.html 的 #ds-exchange 必须是 <select>');
  assert.strictEqual(doc.byId['ds-timeframe'].tagName, 'SELECT', 'index.html 的 #ds-timeframe 必须是 <select>');
  assert.strictEqual(doc.byId['ds-symbol'].tagName, 'INPUT', '#ds-symbol 应是 <input>（.value 恒合法）');

  const settings = o.settings || { general: Object.assign({}, CURSOR) };
  const calls = [];
  const fetchImpl = (url) => new Promise((resolve) => {
    const u = String(url);
    calls.push(u);
    const ok = (b) => resolve({
      ok: true, status: 200,
      json: async () => b, text: async () => '', headers: { get: () => null },
    });
    const bad = () => resolve({ ok: false, status: 500, text: async () => 'boom', headers: { get: () => null } });
    if (u.startsWith('/api/settings')) return o.settingsFail ? bad() : ok(settings);
    if (u.startsWith('/api/tv/exchanges')) return o.exchangesFail ? bad() : ok(o.exchanges || EXCHANGES_PAYLOAD);
    if (u.startsWith('/api/timeframes')) return ok(o.timeframes || TIMEFRAMES_PAYLOAD);
    if (u.includes('/api/bars/next-close')) return ok({ next_close_ts: 1791236100000, seconds_remaining: 30, market_closed: false });
    if (u.startsWith('/api/tv/symbols')) return ok({ symbols: ['NVDA', 'AAPL'] });
    if (u.startsWith('/api/bars')) return ok({ bars: [], meta: {} });
    if (u.startsWith('/api/records')) return ok([]);
    return ok({});
  });

  const nowSec = Math.floor(Date.now() / 1000);
  const store = { pa_token: makeToken({ sub: 'admin', iat: nowSec - 60, exp: nowSec + 3600 }) };
  const storage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  };

  const sandbox = {
    console: { log() {}, warn() {}, error() {}, info() {} },
    setTimeout, clearTimeout, setInterval: () => 0, clearInterval,
    document: doc, fetch: fetchImpl,
    window: { addEventListener() {}, _indicatorsAPI: null },
    localStorage: storage, sessionStorage: storage,
    AbortController, TextDecoder, URL, URLSearchParams, Buffer,
    location: { href: 'http://localhost/', reload() {} },
    navigator: { userAgent: 'node' },
    alert() {}, confirm: () => true, prompt: () => '1',
    requestAnimationFrame: () => 0, Event: function () {}, CustomEvent: function () {},
  };
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(API_JS, 'utf8'), sandbox, { filename: 'api.js' });
  vm.runInContext(fs.readFileSync(APP_JS, 'utf8'), sandbox, { filename: 'app.js' });

  const el = (id) => doc.byId[id];
  async function nextCloseUrl() {
    calls.length = 0;
    await sandbox.fetchAndUpdateNextCloseTs();
    return calls.find((u) => u.includes('next-close')) || '';
  }
  return {
    doc, sandbox, calls, el, nextCloseUrl,
    /** `let` 顶层绑定不挂在 vm 的 globalThis 上，必须在上下文里求值 */
    currentSettings: () => vm.runInContext('currentSettings', sandbox),
    /** 复刻 init 的真实顺序：app.js:522-525 */
    boot: async () => {
      await sandbox.loadSettings();
      await sandbox.loadExchanges();
      await sandbox.loadSymbols();
      await sandbox.loadTimeframes();
    },
    async nextCloseExchange() {
      const url = await nextCloseUrl();
      const m = url.match(/[?&]exchange=([^&]*)/);
      return m ? decodeURIComponent(m[1]) : null;
    },
    /** 读 #switch-error 里真正显示给用户的消息（走真实 showSwitchError） */
    switchError: () => {
      const bar = doc.byId['switch-error'];
      if (!bar) return '';
      const msg = bar.querySelector('.switch-error-msg');
      return msg ? String(msg.textContent) : '';
    },
  };
}

const flush = () => new Promise((r) => setImmediate(r));

/* ══ A. 桩自身：先证明 select 语义是真的，否则下面的断言都是空的 ═════════ */

function testStubImplementsRealSelectSemantics() {
  const s = new El('select', 'x');
  s.innerHTML = '<option value="GATEIO">Gate.io</option><option value="NASDAQ">纳斯达克</option>';
  // 「ask for a reset」：无 selected ⇒ 首项自动选中
  assert.strictEqual(s.value, 'GATEIO', '无 selected 时单选 select 应选中首项');
  s.value = 'NASDAQ';
  assert.strictEqual(s.value, 'NASDAQ', '赋已存在的值应选中它');
  s.value = 'NOT_A_REAL_EXCHANGE';
  assert.strictEqual(s.value, '', '赋不存在的值必须静默变 "" —— 这正是本 bug 的藏身处');
  assert.strictEqual(s.selectedIndex, -1, '赋不存在的值后应无选中项');
  assert.strictEqual(s.optionValues().length, 2, '赋不存在的值不应凭空造出 option');
  // 空壳 select（index.html 的初始状态）
  const empty = new El('select', 'y');
  assert.strictEqual(empty.value, '', '空壳 select 的 value 应为 ""');
  assert.deepStrictEqual(empty.optionValues(), [], '空壳 select 没有 option');
  console.log('✅ A 桩复刻了 select/option 语义（不存在值→""，无 selected→首项）');
}

/* ══ B. 刷新后正常回显：游标是 NASDAQ，.value 必须是 NASDAQ ══════════════ */

async function testRefreshEchoesCursorIntoValue() {
  const p = buildPage();
  await p.boot();

  // 断言的是 **.value**（用户看见的），不是 dataset —— dataset 只是载体
  assert.strictEqual(p.el('ds-exchange').value, 'NASDAQ',
    '刷新后 #ds-exchange.value 必须等于服务端游标 NASDAQ');
  assert.strictEqual(p.el('ds-timeframe').value, '5m',
    '刷新后 #ds-timeframe.value 必须等于服务端游标 5m');
  assert.strictEqual(p.el('ds-symbol').value, 'NVDA', '#ds-symbol 应回显 NVDA');

  // 三处投影必须一致 —— 不允许「dataset 对、.value 错」
  for (const id of ['ds-exchange', 'ds-timeframe']) {
    const e = p.el(id);
    assert.strictEqual(e.dataset.current, e.value,
      `#${id} 的 dataset.current 与 .value 必须一致（唯一真源 = 服务端游标）`);
  }
  const g = p.currentSettings().general;
  assert.strictEqual(g.last_tradingview_exchange, p.el('ds-exchange').value, 'currentSettings 与 .value 必须一致');
  assert.strictEqual(g.last_timeframe, p.el('ds-timeframe').value, 'currentSettings 与 .value 必须一致');

  // 必须是**用户能看见的那个**选项被选中，而不是碰巧 value 字符串相等
  const sel = p.el('ds-exchange').options.filter((o) => o.selected);
  assert.strictEqual(sel.length, 1, '应恰好选中一个 option');
  assert.strictEqual(sel[0].value, 'NASDAQ', '被选中的就是 NASDAQ 那个 option');
  console.log('✅ B 刷新回显：exchange=%s timeframe=%s', p.el('ds-exchange').value, p.el('ds-timeframe').value);
}

/* ══ C. 悬停/非首个选项：不能退化成「首项也算对」 ════════════════════════ */

async function testEchoesNonFirstOption() {
  const p = buildPage({
    settings: { general: { last_symbol: '600519', last_tradingview_exchange: 'SSE', last_timeframe: '1d' } },
  });
  await p.boot();
  assert.strictEqual(p.el('ds-exchange').value, 'SSE', '应回显 SSE（列表第 6 项，不是首项）');
  assert.strictEqual(p.el('ds-timeframe').value, '1d', '应回显 1d');
  assert.strictEqual(p.el('ds-symbol').value, '600519');
  // 若实现退化成「清空后靠首项复位」，这里会拿到 GATEIO / 1m
  assert.notStrictEqual(p.el('ds-exchange').value, 'GATEIO', '不得退化成首项 GATEIO');
  assert.notStrictEqual(p.el('ds-timeframe').value, '1m', '不得退化成首项 1m');
  console.log('✅ C 非首项游标也能精确回显：SSE / 1d');
}

/* ══ D. 选项尚未就位时：挂起，绝不自我破坏 ════════════════════════════════
 * 策略是「挂起」：dataset 先记下，选项就位后由 loadExchanges/loadTimeframes
 * 兑现。这里锁死两件事：
 *   ① 中间那一刻不能产生**假选中**（凭空选一个、或把值写坏）；
 *   ② 兑现后必须精确对上 —— 而兑现只可能来自 dataset，因为 innerHTML 重建
 *      会把 `.value` 整个冲掉。 */

async function testPendingWhileOptionsAbsent() {
  const p = buildPage();
  const ex = p.el('ds-exchange');

  // 前置：此刻选项一个都还没加载（loadSettings 跑在 loadExchanges 之前）
  assert.deepStrictEqual(ex.optionValues(), [], '前置条件：此时 #ds-exchange 尚无 option');

  await p.sandbox.loadSettings();

  // 空壳 <select> 的 .value 恒为 '' —— 这是浏览器语义、无法避免，也不算「写坏」。
  // 真正要防的是**假选中**：此刻必须无选中项，而不是随便落到某个 option 上。
  assert.strictEqual(ex.selectedIndex, -1, '选项未就位时不得产生假选中');
  assert.deepStrictEqual(ex.optionValues(), [], '选项未就位时不得凭空造出 option');
  assert.strictEqual(ex.value, '', '空壳 select 读出来本就是 ""（浏览器语义，非缺陷）');
  assert.strictEqual(ex.dataset.current, 'NASDAQ', '游标必须挂在 dataset.current 上等待兑现');

  // 兑现：选项到位后自动对上（且只能靠 dataset —— innerHTML 重建会冲掉 .value）
  await p.sandbox.loadExchanges();
  assert.strictEqual(ex.value, 'NASDAQ', '选项就位后必须兑现挂起的游标');

  // 周期同理，单独再走一遍（别照抄做法）
  const q = buildPage();
  const qtf = q.el('ds-timeframe');
  assert.deepStrictEqual(qtf.optionValues(), [], '前置：#ds-timeframe 尚无 option');
  await q.sandbox.loadSettings();
  assert.strictEqual(qtf.selectedIndex, -1, '周期也不得产生假选中');
  assert.strictEqual(qtf.dataset.current, '5m', '周期游标也应挂起');
  await q.sandbox.loadTimeframes();
  assert.strictEqual(qtf.value, '5m', '周期选项就位后必须兑现');
  console.log('✅ D 选项未就位→挂起→兑现（exchange 与 timeframe 各走一遍）');
}

/* ══ E. 游标不在选项列表里：不得静默改选，必须报出来 ══════════════════════
 * 「回显不了」有两种表现：停在空、或悄悄落到别的值。**后者更危险** ——
 * 用户以为在看 DELISTED_X，实际按 GATEIO 取数，全程无报错。
 *
 * 这里覆盖的是**选项已就位**的那条路径（applySubscribe 成功后重调
 * loadSettings 的重入）。冷启动那条路径由 `loadExchanges()` 负责，且它的
 * 回落行为是既有代码、不在本会话写集内 —— 见交付说明「已知残留」。 */

async function testUnknownCursorIsReportedNotSilentlyReselected() {
  const p = buildPage({
    // 只让交易所对不上，周期给合法值 —— 否则两条提示会互相覆盖，测的就不是这一条了
    settings: { general: { last_symbol: 'NVDA', last_tradingview_exchange: 'DELISTED_X', last_timeframe: '5m' } },
  });
  // 先让选项就位（模拟 applySubscribe 之后的重入）
  await p.sandbox.loadExchanges();
  await p.sandbox.loadTimeframes();
  const ex = p.el('ds-exchange');
  const before = ex.value;
  // 前置：dataset.current 为空 ⇒ loadExchanges 选中 value="" 的「自动（探测）」。
  // 这正是「静默落到错值」的样本 —— 也是必须上报、不能默默接受的理由。
  assert.strictEqual(before, '', '前置：未回显时落在「自动（探测）」(value="")');

  await p.sandbox.loadSettings();

  // 权威载体仍记着服务端游标 —— UI 的「表达不了」不许篡改真源
  assert.strictEqual(ex.dataset.current, 'DELISTED_X', 'dataset 载体应保留服务端游标 DELISTED_X');
  // 不得被这次回显偷偷改成别的交易所
  assert.strictEqual(ex.value, before, '游标对不上时不得改选成别的交易所');
  // 关键：必须**报出来**，而不是静默
  assert.ok(p.switchError().includes('DELISTED_X'),
    `游标对不上必须提示用户，实际提示：${JSON.stringify(p.switchError())}`);

  // 周期单独一页：否则两条提示会互相覆盖（#switch-error 只有一根）
  const q = buildPage({
    settings: { general: { last_symbol: 'NVDA', last_tradingview_exchange: 'NASDAQ', last_timeframe: '7m' } },
  });
  await q.sandbox.loadExchanges();
  await q.sandbox.loadTimeframes();
  await q.sandbox.loadSettings();
  assert.strictEqual(q.el('ds-timeframe').dataset.current, '7m', '周期载体应保留 7m');
  assert.ok(q.switchError().includes('周期'),
    `周期提示应说明是周期列表，实际：${JSON.stringify(q.switchError())}`);
  console.log('✅ E 未知游标被显式上报而非静默改选：%s / %s', p.switchError(), q.switchError());
}

/* ══ F. next-close 发出去的 exchange 必须与界面一致 ══════════════════════ */

async function testNextCloseUsesEchoedExchange() {
  const p = buildPage();
  await p.boot();
  assert.strictEqual(p.el('ds-exchange').value, 'NASDAQ', '前置：界面应回显 NASDAQ');
  const ex = await p.nextCloseExchange();
  assert.strictEqual(ex, 'NASDAQ', `/api/bars/next-close 的 exchange 应为 NASDAQ，实际 ${JSON.stringify(ex)}`);
  const url0 = await p.nextCloseUrl();
  assert.ok(url0.includes('symbol=NVDA') && url0.includes('timeframe=5m'),
    `另外两轴也必须带上实际游标，实际 URL: ${url0}`);

  // ⚠ 关键场景：下拉框**没有合法选中**（值落到空串 / selectedIndex=-1），
  // 而服务端游标是 NASDAQ。此时必须回落到服务端游标，绝不能把空串发出去 ——
  // 「exchange=」等于告诉后端「交易所不限」，而用户明明在看 NASDAQ。
  const q = buildPage();
  await q.boot();
  q.el('ds-exchange').value = 'BOGUS_NOT_IN_LIST';   // 真机等价：某处把 .value 写坏了
  assert.strictEqual(q.el('ds-exchange').value, '', '前置：写坏后 .value 应为空串');
  assert.strictEqual(q.el('ds-exchange').selectedIndex, -1, '前置：应处于无选中项状态');
  assert.strictEqual(q.currentSettings().general.last_tradingview_exchange, 'NASDAQ',
    '前置：服务端游标仍是 NASDAQ');
  const qEx = await q.nextCloseExchange();
  assert.strictEqual(qEx, 'NASDAQ',
    `DOM 无合法选中时必须回落到服务端游标 NASDAQ，实际发出 ${JSON.stringify(qEx)}`);

  // 反过来：服务端游标本就是空（「自动（探测）」），不得凭空编一个交易所
  const r = buildPage({
    settings: { general: { last_symbol: 'NVDA', last_tradingview_exchange: '', last_timeframe: '5m' } },
  });
  await r.boot();
  assert.strictEqual(r.el('ds-exchange').value, '', '前置：游标为空时下拉停在「自动（探测）」');
  assert.strictEqual(await r.nextCloseExchange(), '', '服务端游标为空时不得编造交易所');
  console.log('✅ F next-close 的 exchange=%s（与界面一致），空选中时回落到服务端游标', ex);
}

/* ══ G. 重入路径：选项已就位时，dataset 与 .value 不得漂移 ════════════════
 * `applySubscribe()` 成功后必须重调 `loadSettings()`（AGENTS.md 硬约束）。
 * 那时选项**早已就位**，用户刚在下拉框里选过东西 —— 此时只更新 dataset 载体、
 * 不碰 .value，两处投影就会各说各话（服务端说 NYSE、界面停在 SSE）。
 * 冷启动那条路径靠「loadExchanges 读 dataset」兜住，这条路径没有第二次
 * loadExchanges，必须当场兑现。 */

async function testReentryKeepsDatasetAndValueInSync() {
  const p = buildPage({
    settings: { general: { last_symbol: 'NVDA', last_tradingview_exchange: 'NYSE', last_timeframe: '1d' } },
  });
  await p.sandbox.loadExchanges();
  await p.sandbox.loadTimeframes();
  // 模拟用户在页面上选了另一个交易所（只有 .value 变，dataset 尚未同步）
  p.el('ds-exchange').value = 'SSE';
  assert.strictEqual(p.el('ds-exchange').value, 'SSE', '前置：用户选中 SSE');
  assert.notStrictEqual(p.el('ds-exchange').dataset.current, 'SSE', '前置：dataset 此刻还没跟上');

  await p.sandbox.loadSettings();

  assert.strictEqual(p.el('ds-exchange').value, 'NYSE',
    '选项已就位时必须当场兑现 .value（不得停在 SSE）');
  assert.strictEqual(p.el('ds-exchange').dataset.current, 'NYSE', 'dataset 载体也要跟上');
  assert.strictEqual(p.el('ds-exchange').value, p.el('ds-exchange').dataset.current,
    '两处投影必须一致');
  // 周期同理
  assert.strictEqual(p.el('ds-timeframe').value, '1d', '周期也必须当场兑现');
  console.log('✅ G 重入不漂移：exchange=%s timeframe=%s',
    p.el('ds-exchange').value, p.el('ds-timeframe').value);
}

/* ══ 运行全部 ═══════════════════════════════════════════════════════════ */

(async () => {
  const cases = [
    ['A select 语义', testStubImplementsRealSelectSemantics],
    ['B 刷新回显', testRefreshEchoesCursorIntoValue],
    ['C 非首项游标', testEchoesNonFirstOption],
    ['D 选项未就位', testPendingWhileOptionsAbsent],
    ['E 未知游标', testUnknownCursorIsReportedNotSilentlyReselected],
    ['F next-close', testNextCloseUsesEchoedExchange],
    ['G 重入不漂移', testReentryKeepsDatasetAndValueInSync],
  ];
  let failed = 0;
  for (const [name, fn] of cases) {
    try {
      await fn();
    } catch (e) {
      failed += 1;
      console.error(`❌ ${name}: ${e.message}`);
    }
  }
  if (failed) {
    console.error(`\n${failed}/${cases.length} 条失败`);
    process.exit(1);
  }
  console.log(`\n全部 ${cases.length} 条通过`);
})();
