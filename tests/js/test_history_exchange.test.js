/* 历史列表项的**交易所标注**。
 *
 * 运行：node tests/js/test_history_exchange.test.js
 *
 * ## 要守护的病
 *
 * 线上现象：页面订阅 `NASDAQ / NVDA / 5m`，历史列表里那条却显示成 `NVDA·5m`
 * —— **看不出它是 GATEIO 下的**。于是「当前品类查不到、全部品种查得到」这个
 * 现象完全无法自查，用户只能来问。根因在写库（取错会话游标）另有会话修；
 * 本文件只锁死展示层：**同 symbol+timeframe、不同交易所的记录必须长得不一样**。
 *
 * ## 为什么真的执行 app.js，而不是把渲染逻辑抄一份出来测
 *
 * `renderHistoryList` 依赖模块级的 `exchangeLabels`（由 `loadExchanges()` 从
 * `/api/tv/exchanges` 填），它俩之间的连线就是本测试的主体。把这段抄成纯函数
 * 测，抄的人会顺手把「标签从哪来」也一起编出来 —— 于是测的是一个现实中不存在
 * 的世界。故这里用最小 DOM 桩 + `vm` 跑**真实** api.js / app.js：
 * fetch 桩按 URL 分发，喂真实形状的响应。
 *
 * 样本形状逐字段取自 `web/api/routes_records.py::_list_records` 的返回结构
 * （字段名是 `exchange`，值是裸代号如 `GATEIO`，**不是**可读名）。
 * 可读名的真源是 `web/api/routes_data.py::list_tv_exchanges` 的 `label_map`
 * （`GATEIO → Gate.io`、`SSE → 上交所`…），此处按原样抄进 fetch 桩。
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
const STYLE_CSS = path.join(ROOT, 'web', 'static', 'css', 'style.css');

/* ══ 1. 最小 DOM 桩 ═══════════════════════════════════════════════════════
 * 必须实现 escapeHtml 依赖的语义：`createElement('div')` 后写 textContent
 * 再读 innerHTML，要拿到**转义后的文本**。桩里漏掉这一条，注入用例就会
 * 因为「什么都渲染不出来」而假绿。 */

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
    this.value = '';
    this.title = '';
    this.hidden = false;
    this.disabled = false;
  }
  set className(v) { this.classList._s = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return this.classList.value; }
  set innerHTML(v) { this._html = String(v); this._text = null; }
  get innerHTML() { return this._text !== null ? escapeText(this._text) : this._html; }
  set textContent(v) { this._text = v === null ? '' : String(v); this._html = ''; }
  get textContent() { return this._text !== null ? this._text : this._html.replace(/<[^>]*>/g, ''); }
  addEventListener(t, f) { (this._listeners[t] = this._listeners[t] || []).push(f); }
  removeEventListener(t, f) {
    const arr = this._listeners[t] || [];
    const i = arr.indexOf(f);
    if (i >= 0) arr.splice(i, 1);
  }
  fire(t) {
    if (t === 'click' && this.tagName === 'INPUT' && this.attrs.type === 'checkbox') {
      this.checked = !this.checked;
    }
    const ev = { type: t, target: this, stopPropagation() {}, preventDefault() {} };
    (this._listeners[t] || []).slice().forEach((f) => f.call(this, ev));
    return ev;
  }
  appendChild(c) { this.children.push(c); return c; }
  contains(n) { return n === this || this.children.some((c) => c.contains && c.contains(n)); }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  getAttribute(k) { return this.attrs[k]; }
  setAttribute(k, v) { this.attrs[k] = v; }
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
  /** `#history-list` 这类**静态**元素必须在脚本跑之前就存在（它们写在 HTML 里）。
   *  查不到返回 null 让测试红；反过来，index.html 里不存在的东西也必须查不到
   *  —— 桩不替实现补元素。 */
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
    if (sel.startsWith('#')) {
      const root = this.byId[sel.slice(1)];
      if (!root) return [];
      const out = [];
      (function walk(n) { out.push(n); n.children.forEach(walk); })(root);
      return out;
    }
    if (sel.startsWith('.')) {
      const c = sel.slice(1);
      return this.all.filter((e) => String(e.attrs.class || '').split(/\s+/).includes(c));
    }
    return this.all.filter((e) => e.tagName === sel.toUpperCase());
  }
  getElementById(id) { return this.byId[id] || null; }
}

/* ══ 2. 样本 ═════════════════════════════════════════════════════════════
 * 字段名与 `/api/records` 的 200 响应体（裸数组）逐字段对齐。 */

/** 可读名映射：与 `web/api/routes_data.py::list_tv_exchanges` 的 label_map 同源。 */
const EXCHANGE_LABELS = {
  GATEIO: 'Gate.io', BINANCE: 'Binance', BYBIT: 'Bybit', OKX: 'OKX',
  BITSTAMP: 'Bitstamp', COINBASE: 'Coinbase', OANDA: 'OANDA',
  PEPPERSTONE: 'Pepperstone', FOREXCOM: 'FOREX.com',
  TVC: 'TVC（TradingView 自有）', CAPITALCOM: 'Capital.com',
  SSE: '上交所', SZSE: '深交所', HKEX: '港交所', SP: 'S&P',
  NYSE: '纽交所', NASDAQ: '纳斯达克', CBOT: 'CBOT', CME_MINI: 'CME Mini',
  '': '自动（探测）',
};
const EXCHANGES_PAYLOAD = Object.entries(EXCHANGE_LABELS).map(([id, label]) => ({ id, label }));

/** 生产库实况（records/pa_agent.db 只读查询）：NVDA 在 NASDAQ 与 GATEIO 下都有记录，
 *  且同 code 不同交易所 —— 正是这条样本要复现的场景。 */
const SAMPLE = [
  {
    record_id: 'GATEIO/NVDA/5m/2026-10-05_20-00-28_456_e6337f',
    timestamp: '2026-10-05T20:00:28.456+00:00',
    symbol: 'NVDA', timeframe: '5m', exchange: 'GATEIO',
    order_type: '限价单', direction: '做多', terminal_outcome: null,
    partial_reason: null, has_exception: false,
    last_close_bar_iso: '2026-10-05T19:55:00.000+00:00',
    anchor_bar_ts_ms: 1791226500000, incremental: false, continuous: false,
  },
  {
    record_id: 'NASDAQ/NVDA/5m/2026-10-04_17-10-13_001_a1b2c3',
    timestamp: '2026-10-04T17:10:13.001+00:00',
    symbol: 'NVDA', timeframe: '5m', exchange: 'NASDAQ',
    order_type: '限价单', direction: '做空', terminal_outcome: null,
    partial_reason: null, has_exception: false,
    last_close_bar_iso: '2026-10-04T17:05:00.000+00:00',
    anchor_bar_ts_ms: 1791046800000, incremental: true, continuous: false,
  },
  {
    record_id: 'SSE/600519/1d/2026-10-03_09-30-00_777_deadbe',
    timestamp: '2026-10-03T09:30:00.777+00:00',
    symbol: '600519', timeframe: '1d', exchange: 'SSE',
    order_type: 'no_order', direction: null, terminal_outcome: 'no_trade',
    partial_reason: null, has_exception: false,
    last_close_bar_iso: '2026-10-03T07:00:00.000+00:00',
    anchor_bar_ts_ms: 1791006000000, incremental: false, continuous: true,
  },
];

/** 宿主数据里的注入串：裸代号字段同样走渲染，不能因为「它是交易所名」就豁免。 */
const HOSTILE = `<img src=x onerror="alert('x')">&"'`;
const HOSTILE_REC = {
  record_id: "EVIL/1d/2026-10-03_09-30-00_001_cafebe",
  timestamp: '2026-10-03T09:30:00.000+00:00',
  symbol: 'X', timeframe: '1d', exchange: HOSTILE,
  order_type: '限价单', direction: '做多', terminal_outcome: null,
  partial_reason: null, has_exception: false,
  last_close_bar_iso: null, anchor_bar_ts_ms: 1791006000000,
  incremental: false, continuous: false,
};

/* ══ 3. 装载真实 app.js ═══════════════════════════════════════════════════ */

/** 挂一枚有效令牌（形状与 `pa_agent/storage/auth.py::issue_token` 同形），否则
 *  api.js 的闸门会在发出任何请求之前就抛「需要登录」。 */
function makeToken(claims) {
  const b64 = (o) => Buffer.from(JSON.stringify(o), 'utf8').toString('base64')
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  return `v1.${b64(claims)}.${b64({ sig: true })}`;
}

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
  const setVal = (id, v) => { if (doc.byId[id]) doc.byId[id].value = v; };
  setVal('ds-exchange', o.exchange || 'NASDAQ');
  setVal('ds-symbol', o.symbol || 'NVDA');
  setVal('ds-timeframe', o.timeframe || '5m');
  doc.byId['history-list'].innerHTML = '<div class="history-empty muted-text">暂无历史记录</div>';

  const nowSec = Math.floor(Date.now() / 1000);
  const store = { pa_token: makeToken({ sub: o.user || 'admin', iat: nowSec - 60, exp: nowSec + 3600 }) };
  const storage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  };

  // 交易所端点可被用例替换（注入用例要喂一个恶意 label）
  const exchanges = o.exchanges || EXCHANGES_PAYLOAD;
  const exchangesFail = !!o.exchangesFail;

  const calls = [];
  const fetchImpl = (url) => new Promise((resolve, reject) => {
    const u = String(url);
    const entry = { url: u, _resolve: resolve, _reject: reject };
    calls.push(entry);
    const ok = (body) => resolve({
      ok: true, status: 200,
      json: async () => body, text: async () => '', headers: { get: () => null },
    });
    if (u.startsWith('/api/tv/exchanges')) {
      if (exchangesFail) resolve({ ok: false, status: 500, text: async () => 'boom' });
      else ok(exchanges);
      return;
    }
    if (o.autoResolve) o.autoResolve(entry, ok);
  });

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

  return {
    doc, sandbox, calls,
    list: () => doc.byId['history-list'].innerHTML,
    /** 走真实路径把 label 映射装上（不手工写模块内部变量） */
    loadExchangeLabels: async () => { await sandbox.loadExchanges(); },
    render: (records, showSymbol) => sandbox.renderHistoryList(records, { showSymbol }),
  };
}

const flush = () => new Promise((r) => setImmediate(r));
/** 取所有交易所徽标的文本（按条目顺序） */
const badges = (html) => [...html.matchAll(/<span class="history-item-exchange">([\s\S]*?)<\/span>/g)]
  .map((m) => m[1]);
const items = (html) => (html.match(/class="history-item"/g) || []).length;

/* ══ A. 勾「全部品种」时每条都带可读交易所名 ══════════════════════════════ */
async function testBrowseAllShowsReadableExchange() {
  const page = buildPage();
  await page.loadExchangeLabels();
  page.render(SAMPLE, true);
  const html = page.list();

  assert.strictEqual(items(html), SAMPLE.length, `应渲染 ${SAMPLE.length} 条，实际 ${items(html)} 条`);
  assert.deepStrictEqual(badges(html), ['Gate.io', '纳斯达克', '上交所'],
    `交易所必须用后端 label_map 的可读名（不得裸代号），实际：${JSON.stringify(badges(html))}`);

  // 同 symbol+timeframe 不同交易所的两条必须**长得不一样**（本次要修的病）
  const itemBlocks = html.split('<div class="history-item"').slice(1);
  const a = itemBlocks[0].replace(/<[^>]*>/g, '');
  const b = itemBlocks[1].replace(/<[^>]*>/g, '');
  assert.ok(!a.includes('纳斯达克') && !b.includes('Gate.io'),
    '两条记录各自只带自己的交易所，不能串');
  assert.ok(/NVDA·5m/.test(itemBlocks[0]) && /NVDA·5m/.test(itemBlocks[1]),
    '两条的 symbol·timeframe 文本应完全相同 —— 这正是它们此前无法分辨的原因');

  // 徽标必须与 symbol 徽标同属一条，且有独立 class 供样式弱化
  assert.ok(itemBlocks[0].includes('history-item-exchange'), '徽标要有独立 class');
  console.log('✅ A 可读名标注：%s', badges(html).join(' / '));
}

/* ══ B. 注入防护：exchange 与 label 都必须转义 ═══════════════════════════ */
/** 整段 innerHTML 里所有标签的属性 —— 判断「有没有注入出可执行属性」时只看标签，
 *  因为转义后的**文本**里本来就会出现 onerror= 这几个字符（那是无害的）。 */
const tagAttrs = (html) => (html.match(/<[^>]*>/g) || []);
const assertNoInjectedAttr = (html, where) => {
  const bad = tagAttrs(html).filter((t) => /<[^>]+\son\w+\s*=/i.test(t));
  assert.strictEqual(bad.length, 0, `${where}：标签里出现了事件属性 → ${bad.join(' | ')}`);
};

async function testExchangeIsEscaped() {
  // B1：裸代号字段本身带注入串（后端 label_map 未收录 → 回落裸代号）
  const page = buildPage();
  await page.loadExchangeLabels();
  page.render([HOSTILE_REC], true);
  let html = page.list();
  let [badge] = badges(html);
  assert.ok(badge, '注入串也必须渲染出徽标（不能因为「长得像攻击」就整个不显示）');
  // 逐字等于转义后的原串：`& < > " '` 五类一个都不能漏
  assert.strictEqual(badge, escapeText(HOSTILE),
    `裸 exchange 必须整体转义（期望 ${escapeText(HOSTILE)}，实际 ${badge}）`);
  for (const ent of ['&lt;', '&quot;', '&#39;', '&amp;']) {
    assert.ok(badge.includes(ent), `缺少转义实体 ${ent}，实际：${badge}`);
  }
  assert.ok(!/<img/i.test(html), `整段 innerHTML 里出现了未转义的 <img>：${html}`);
  assertNoInjectedAttr(html, 'B1 裸 exchange');

  // B2：恶意 label（映射本身就带 HTML）—— 只转义裸 exchange 是不够的
  const page2 = buildPage({
    exchanges: [{ id: 'EVIL', label: `<b onmouseover=alert(1)>${HOSTILE}</b>` }],
  });
  await page2.loadExchangeLabels();
  page2.render([{ ...HOSTILE_REC, exchange: 'EVIL' }], true);
  html = page2.list();
  const [badge2] = badges(html);
  assert.strictEqual(badge2, escapeText(`<b onmouseover=alert(1)>${HOSTILE}</b>`),
    `映射里的 label 也必须整体转义，实际：${badge2}`);
  assert.ok(!/<b\s+onmouseover/i.test(html), `未转义的 <b onmouseover> 进入了 DOM：${html}`);
  assertNoInjectedAttr(html, 'B2 恶意 label');

  // B3：AGENTS.md 硬约束 —— 外部数据插值不得内联 onclick
  const page3 = buildPage();
  await page3.loadExchangeLabels();
  page3.render(SAMPLE, true);
  assert.ok(!/onclick=/i.test(page3.list()),
    `渲染结果里出现了内联 onclick：${page3.list()}`);
  assertNoInjectedAttr(page3.list(), 'B3 正常样本');
  console.log('✅ B 注入防护：%s', badge2.slice(0, 60));
}

/* ══ C. 「当前品类」模式不显示交易所 ═══════════════════════════════════════ */
async function testCurrentScopeHidesExchange() {
  const page = buildPage();
  await page.loadExchangeLabels();
  page.render(SAMPLE, false);           // showSymbol=false ← 过滤已锁定三元组
  const html = page.list();
  assert.ok(!html.includes('history-item-exchange'), '当前品类模式不应渲染交易所徽标');
  assert.deepStrictEqual(badges(html), [], `当前品类模式不应出现交易所文本：${JSON.stringify(badges(html))}`);
  for (const label of ['Gate.io', '纳斯达克', '上交所']) {
    assert.ok(!html.includes(label), `当前品类模式不应出现可读名 ${label}`);
  }
  // 标的徽标同样不出现（该模式过滤已锁定，重复显示是噪音）
  assert.ok(!html.includes('history-item-symbol'), '当前品类模式本就不显示 symbol');
  assert.strictEqual(items(html), SAMPLE.length, '条目本身仍要渲染，只是少了两个徽标');
  console.log('✅ C 当前品类模式无交易所标注（%d 条）', items(html));
}

/* ══ D. 降级：映射未加载 / 未收录时回落到裸代号，绝不留空 ═════════════════ */
async function testDegradesToRawCode() {
  // D1：/api/tv/exchanges 失败（后端 500 / 未登录）→ 仍显示裸代号
  const page = buildPage({ exchangesFail: true });
  await page.loadExchangeLabels();
  page.render(SAMPLE, true);
  let html = page.list();
  assert.deepStrictEqual(badges(html), ['GATEIO', 'NASDAQ', 'SSE'],
    `映射加载失败必须回落裸代号而不是空白，实际：${JSON.stringify(badges(html))}`);

  // D2：后端新增了 label_map 未收录的交易所 → 同上
  const page2 = buildPage();
  await page2.loadExchangeLabels();
  page2.render([{ ...SAMPLE[0], exchange: 'BRAND_NEW_EX' }], true);
  assert.deepStrictEqual(badges(page2.list()), ['BRAND_NEW_EX'],
    '未收录的交易所必须回落裸代号（可读名表不是封闭词表）');

  // D3：exchange 缺失/空 → 不渲染空徽标（空徽标是视觉噪音）
  const page3 = buildPage();
  await page3.loadExchangeLabels();
  page3.render([{ ...SAMPLE[0], exchange: '' }, { ...SAMPLE[1], exchange: undefined }], true);
  assert.deepStrictEqual(badges(page3.list()), [], '空 exchange 不应渲染空徽标');
  console.log('✅ D 降级路径：%s / %s', badges(html).join(' '), badges(page2.list())[0]);
}

/* ══ E. 静态契约：样式 + 版本号 ═══════════════════════════════════════════ */
async function testStaticContract() {
  const css = fs.readFileSync(STYLE_CSS, 'utf8');
  const m = css.match(/\.history-item-exchange\s*\{([^}]*)\}/);
  assert.ok(m, 'style.css 必须有 .history-item-exchange 规则 —— 否则徽标是纯文本，无弱化层级');
  assert.ok(/var\(--text-dim/.test(m[1]),
    `交易所样式必须沿用既有 --text-dim 变量，不得新造配色：${m[1]}`);
  assert.ok(/var\(--border/.test(m[1]) || /background:/.test(m[1]),
    `交易所样式应沿用既有边框/底色语汇：${m[1]}`);
  const sym = css.match(/\.history-item-symbol\s*\{([^}]*)\}/);
  assert.ok(sym, '.history-item-symbol 应仍然存在（本次改动不得删掉它）');
  assert.ok(css.indexOf('.history-item-exchange') > css.indexOf('.history-item-symbol'),
    '交易所样式应与 .history-item-symbol 相邻（同一视觉分组）');

  const html = fs.readFileSync(INDEX_HTML, 'utf8');
  const js = html.match(/app\.js\?v=(\d+)/);
  const cssV = html.match(/style\.css\?v=(\d+)/);
  assert.ok(js && cssV, 'index.html 必须给 app.js / style.css 都加 ?v= 版本号');
  assert.ok(Number(js[1]) >= 70,
    `app.js?v 必须 ≥70（本次改动前是 69），实际 ${js[1]} —— 不递增就等于没改，用户看到的仍是旧版`);
  assert.ok(Number(cssV[1]) >= 43,
    `style.css?v 必须 ≥43（本次改动前是 42），实际 ${cssV[1]}`);
  console.log('✅ E 静态契约：app.js?v=%s / style.css?v=%s', js[1], cssV[1]);
}

/* ══ 跑全部 ═══════════════════════════════════════════════════════════════ */
const SUITES = [
  testBrowseAllShowsReadableExchange,
  testExchangeIsEscaped,
  testCurrentScopeHidesExchange,
  testDegradesToRawCode,
  testStaticContract,
];

(async () => {
  for (const fn of SUITES) {
    await fn();
    await flush();
  }
  console.log('\n✅ 全部 %d 组用例通过（历史列表交易所标注）', SUITES.length);
})().catch((e) => {
  console.error('\n❌ 用例失败：%s\n%s', e.message, e.stack);
  process.exit(1);
});

setTimeout(() => {
  console.error('\n❌ 用例未在预期时间内结束');
  process.exit(1);
}, 20000).unref?.();