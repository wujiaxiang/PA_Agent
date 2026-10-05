/* 「全部品种」历史列表的回归测试。
 *
 * 运行：node tests/js/test_history_list.test.js
 *
 * ## 为什么必须是「执行真实 app.js」而不是切片纯逻辑
 *
 * 这个 bug 的整条因果链横跨三个地方 —— 请求 URL 的拼法、`await` 的时序、
 * 以及渲染函数对 `#history-list` 的**就地覆写**。任何只切片某一个函数的
 * 写法都会把真正出问题的那一段（并发交错）从测试里拿掉，于是测试永远绿。
 * 故本文件用一套最小 DOM 桩 + `vm` **真的把 api.js 与 app.js 跑起来**，
 * 喂进**真实 `/api/records?limit=50` 的响应样本**（样本由
 * `pa_agent.storage.repositories` + `web.api.routes_records` 在隔离库里生成，
 * 形状与生产逐字段一致），再断言 `#history-list` 的 innerHTML。
 *
 * ## 守护的四条契约
 *   A. 「全部品种」勾选态下真的渲染出条目（不是「暂无历史记录」）
 *   B. 未勾选时请求带 exchange/symbol/timeframe 三者齐全
 *   C. **过期响应不得覆盖新响应**（本 bug 的根因）
 *   D. 读失败必须与「确实为空」渲染成不同的空态
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

/* ══ 1. 最小 DOM 桩 ══════════════════════════════════════════════════════
 * 必须实现 escapeHtml 所依赖的语义：`createElement('div')` 后写 textContent
 * 再读 innerHTML，要拿到**转义后的文本**。桩里少实现这一条，渲染结果就会
 * 全是空字符串 —— 那时测试绿着，测的却不是它声称的东西。 */

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
    this.style = makeStyle();
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
  /** 触发一次点击。**按 HTML 规范**在派发 click 前先跑 pre-click activation
   *  （勾选框的 checked 就在这一刻翻转）—— 与真人点击的时序一致。 */
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
    this.all = [];          // index.html 里出现过的全部元素（供 .cls / tag 选择器）
    this.listeners = {};
    this.title = '';
  }
  createElement(t) { return new El(t); }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  /** `#history-list` / `#chk-history-all-symbols` 这类**静态**元素必须真的存在
   * （它们在脚本跑之前就在 HTML 里）；查不到返回 null，让测试红。
   * 反过来，index.html 里根本不存在的东西也必须查不到 —— 桩不替实现补元素。 */
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

/* ══ 2. 真实响应样本 ══════════════════════════════════════════════════════
 * 形状逐字段取自 `web/api/routes_records.py::_list_records` 的返回结构
 * （`GET /api/records?limit=50` 的 200 响应体是**裸数组**，不是 {data: [...]}）。
 * 用例 A 会显式断言这一点 —— api.js 一旦改成包一层解包，records.length
 * 就变成 undefined，列表会静默空掉。 */

const SAMPLE = [
  {
    record_id: 'GATEIO/BTCUSDT/1h/2026-10-05_20-00-28_456_e6337f',
    timestamp: '2026-10-05T20:00:28.456+00:00',
    symbol: 'BTCUSDT', timeframe: '1h', exchange: 'GATEIO',
    order_type: '限价单', direction: '做多', terminal_outcome: null,
    partial_reason: null, has_exception: false,
    last_close_bar_iso: '2026-10-05T19:00:00.000+00:00',
    anchor_bar_ts_ms: 1791226800000, incremental: false, continuous: false,
  },
  {
    record_id: 'NASDAQ/NVDA/5m/2026-10-04_17-10-13_001_a1b2c3',
    timestamp: '2026-10-04T17:10:13.001+00:00',
    symbol: 'NVDA', timeframe: '5m', exchange: 'NASDAQ',
    order_type: '限价单', direction: '做多', terminal_outcome: null,
    partial_reason: null, has_exception: false,
    last_close_bar_iso: '2026-10-04T17:00:00.000+00:00',
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

/* ══ 3. 装载真实 app.js ═══════════════════════════════════════════════════ */

/** 挂一枚有效令牌（形状与 pa_agent/storage/auth.py::issue_token 同形）。
 *  没有它 api.js 的闸门会在**发出任何请求之前**就抛「需要登录」，
 *  loadHistoryList 会走 catch 分支 —— 那测的就不是我们想测的东西了。 */
function makeToken(claims) {
  const b64 = (o) => Buffer.from(JSON.stringify(o), 'utf8').toString('base64')
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  return `v1.${b64(claims)}.${b64({ sig: true })}`;
}

function buildPage(opts) {
  const o = opts || {};
  const doc = new Doc();

  // DOM 骨架直接从 **真实 index.html** 里扫出来 —— 而不是在这里手抄一份。
  // 手抄的清单一漂移，`bindEvents()` 就会因为某个元素「不存在」而中途抛，
  // 后面所有绑定（包括历史的）都没接上，测试却看不出来。
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

  // 真实嵌套：popover 的 header 里有 label，checkbox 裹在 label 里
  // （index.html:137）。document 级「点外部关闭」判定用的是 pop.contains(e.target)，
  // 层级错了会测到另一条路径。
  const pop = doc.byId['history-popover'];
  const chk = doc.byId['chk-history-all-symbols'];
  const header = new El('div');
  const label = new El('label');
  pop.children.length = 0;
  pop.appendChild(header);
  header.appendChild(label);
  header.appendChild(doc.byId['btn-history-refresh']);
  header.appendChild(doc.byId['history-list']);
  label.appendChild(chk);
  pop.classList.add('hidden');

  const setVal = (id, v) => { if (doc.byId[id]) doc.byId[id].value = v; };
  setVal('ds-exchange', o.exchange || 'NASDAQ');
  setVal('ds-symbol', o.symbol || 'NVDA');
  setVal('ds-timeframe', o.timeframe || '5m');
  // 列表初始内容与 index.html 一致（静态占位）
  doc.byId['history-list'].innerHTML = '<div class="history-empty muted-text">暂无历史记录</div>';

  const nowSec = Math.floor(Date.now() / 1000);
  const store = { pa_token: makeToken({ sub: o.user || 'admin', iat: nowSec - 60, exp: nowSec + 3600 }) };
  const storage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  };

  const calls = [];      // [{url, _resolve(payload), _reject(err)}]
  const fetchImpl = (url) => new Promise((resolve, reject) => {
    const entry = { url: String(url), _resolve: resolve, _reject: reject };
    calls.push(entry);
    if (o.autoResolve) o.autoResolve(entry);
  });

  const sandbox = {
    console: { log() {}, warn() {}, error() {}, info() {} },
    setTimeout, clearTimeout,
    setInterval: () => 0, clearInterval,
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

  return { doc, sandbox, calls, list: () => doc.byId['history-list'].innerHTML };
}

const items = (html) => (html.match(/class="history-item"/g) || []).length;
const isEmptyState = (html) => html.includes('暂无历史记录');
/** 让挂起的 promise 回调（await 的续体）跑完。 */
const flush = () => new Promise((r) => setImmediate(r));

/* ══ A. 「全部品种」勾选态真的渲染出条目 ═══════════════════════════════════ */
(async function testBrowseAllRendersItems() {
  const page = buildPage({
    autoResolve: (c) => {
      const u = new URL(c.url, 'http://x');
      // 生产实测：勾「全部品种」= 不带任何过滤条件；带过滤的当前品种请求返回 0 行
      const body = u.searchParams.get('symbol')
        ? []                                     // NVDA/5m/NASDAQ 库里零行
        : SAMPLE;                                // ?limit=50 → 25 条（样本取 3 条）
      c._resolve({ ok: true, status: 200, json: async () => body, text: async () => '', headers: { get: () => null } });
    },
  });
  page.doc.byId['chk-history-all-symbols'].checked = true;
  await page.sandbox.loadHistoryList();
  await flush();

  const html = page.list();
  assert.strictEqual(isEmptyState(html), false,
    '「全部品种」拿到 3 条记录却渲染成空态 —— 正是线上「接口 200 但列表为空」');
  assert.strictEqual(items(html), SAMPLE.length, `应渲染 ${SAMPLE.length} 条，实际 ${items(html)} 条`);
  // 裸数组契约：API.get 若改成包一层 {data: [...]}，records.length 变 undefined
  assert.ok(page.calls[0].url === '/api/records?limit=50',
    `「全部品种」必须不带过滤条件，实际请求 ${page.calls[0].url}`);
  // 跨品种浏览必须显示归属标的，否则一堆条目无法区分
  assert.ok(html.includes('BTCUSDT') && html.includes('NVDA') && html.includes('600519'),
    '跨品种浏览时每条都要显示 symbol');
  assert.ok(html.includes('增量'), 'incremental=true 的记录要打「增量」标记');
  console.log('✅ A 「全部品种」渲染出 %d 条条目（真实响应样本）', items(html));
})();

/* ══ B. 未勾选时过滤条件三者齐全 ═══════════════════════════════════════════ */
(async function testFilteredScope() {
  const page = buildPage({ autoResolve: (c) => c._resolve({ ok: true, status: 200, json: async () => [], text: async () => '', headers: { get: () => null } }) });
  page.doc.byId['chk-history-all-symbols'].checked = false;
  await page.sandbox.loadHistoryList();
  await flush();
  assert.strictEqual(page.calls[0].url,
    '/api/records?exchange=NASDAQ&symbol=NVDA&timeframe=5m&limit=50',
    `未勾选时必须带齐三个过滤条件，实际 ${page.calls[0].url}`);
  assert.ok(isEmptyState(page.list()), '零行时确实应该显示空态');
  console.log('✅ B 未勾选 → %s', page.calls[0].url);
})();

/* ══ C. 过期响应不得覆盖新响应（本 bug 的根因）════════════════════════════
 * 复刻线上时序：先发「当前品种」请求（慢，返回 0 行），再发「全部品种」
 * 请求（快，返回 25 条）。谁后到谁写 DOM —— 没有守卫时，0 行会抹掉 25 条。 */
(async function testStaleResponseDoesNotOverwrite() {
  const page = buildPage({ exchange: 'NASDAQ', symbol: 'NVDA', timeframe: '5m' });
  const chk = page.doc.byId['chk-history-all-symbols'];
  page.sandbox.bindEvents();          // 走真实的绑定路径，不手工调 loadHistoryList

  // T0：打开 popover（未勾选）→ 过滤请求起飞，**不 resolve**
  chk.checked = false;
  page.doc.byId['btn-history'].fire('click');
  await flush();
  assert.ok(page.calls.some((c) => c.url.includes('symbol=NVDA')), '打开 popover 应发出当前品种请求');

  // T1：用户勾「全部品种」→ 第二个请求起飞并**立刻**返回 25 条
  chk.fire('click');                      // 走真实的事件路径（含 pre-click activation）
  await flush();
  const allReq = page.calls.find((c) => c.url === '/api/records?limit=50');
  assert.ok(allReq, '勾选「全部品种」后必须发出 /api/records?limit=50');
  allReq._resolve({ ok: true, status: 200, json: async () => SAMPLE, text: async () => '', headers: { get: () => null } });
  await flush(); await flush();
  assert.strictEqual(items(page.list()), SAMPLE.length, '?limit=50 的响应必须渲染出来');

  // T3：慢的「当前品种」响应终于到达（库里 NVDA 零行 → 空数组）
  page.calls[0]._resolve({ ok: true, status: 200, json: async () => [], text: async () => '', headers: { get: () => null } });
  await flush(); await flush();

  const html = page.list();
  assert.strictEqual(isEmptyState(html), false,
    '过期的「当前品种」空响应覆盖掉了已渲染的「全部品种」列表 —— 就是线上那个 bug');
  assert.strictEqual(items(html), SAMPLE.length,
    `过期响应后条目数应为 ${SAMPLE.length}，实际 ${items(html)}`);
  console.log('✅ C 过期响应被丢弃，仍是 %d 条', items(html));
})();

/* ══ D. 读失败 ≠ 「确实为空」 ═════════════════════════════════════════════ */
(async function testFailureIsNotEmptyState() {
  const page = buildPage({
    autoResolve: (c) => c._resolve({ ok: false, status: 500, text: async () => 'boom' }),
  });
  page.doc.byId['chk-history-all-symbols'].checked = true;
  await page.sandbox.loadHistoryList();
  await flush();
  const html = page.list();
  assert.strictEqual(isEmptyState(html), false,
    '读失败不能渲染成「暂无历史记录」—— 那是「确实没有记录」的说法');
  assert.ok(/加载失败/.test(html), `读失败应渲染独立空态，实际：${html.slice(0, 120)}`);
  console.log('✅ D 读失败渲染独立空态：%s', html.replace(/<[^>]*>/g, '').trim());
})();

/* ══ E. popover / 勾选框的事件绑定确实接到 loadHistoryList ═══════════════
 * index.html 把 checkbox 包在 <label> 里，app.js 绑的是 click。这条锁死
 * 「勾选 → 重新拉列表」这条链路本身没断（回归过：绑定点被删也会红）。 */
(async function testEventBindingsWired() {
  const page = buildPage({ autoResolve: (c) => c._resolve({ ok: true, status: 200, json: async () => SAMPLE, text: async () => '', headers: { get: () => null } }) });
  const html = fs.readFileSync(INDEX_HTML, 'utf8');
  const m = html.match(/<script src="\/js\/app\.js\?v=(\d+)"><\/script>/);
  assert.ok(m, 'index.html 必须给 app.js 加 ?v= 版本号（静态资源缓存兜底）');

  page.sandbox.bindEvents();
  await flush();

  // 1) 勾选框 → 不带过滤的请求
  page.doc.byId['chk-history-all-symbols'].fire('click');
  await flush();
  assert.ok(page.calls.some((c) => c.url === '/api/records?limit=50'),
    '勾选「全部品种」必须触发 /api/records?limit=50');

  // 2) 历史按钮 → 打开 popover 并触发带过滤的请求
  page.calls.length = 0;
  page.doc.byId['chk-history-all-symbols'].checked = false;
  page.doc.byId['btn-history'].fire('click');
  await flush();
  assert.strictEqual(page.doc.byId['history-popover'].classList.contains('hidden'), false,
    '点「历史」必须打开 popover');
  assert.ok(page.calls.some((c) => c.url.includes('symbol=NVDA')),
    '打开 popover 必须拉当前品种的历史');
  console.log('✅ E 事件绑定正常（app.js?v=%s）', m[1]);
})();

setTimeout(() => {
  console.error('\n❌ 用例未在预期时间内结束');
  process.exit(1);
}, 20000).unref?.();