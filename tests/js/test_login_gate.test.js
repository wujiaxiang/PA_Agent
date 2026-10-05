/* 登录闸门的纯逻辑回归测试。
 *
 * 运行：node tests/js/test_login_gate.test.js
 *
 * 范围限定：本文件**只**求值 api.js 里 `== BEGIN PURE-AUTH-LOGIC ==` 与
 * `== END PURE-AUTH-LOGIC ==` 之间的那一段（无 DOM、无 storage、无 fetch）。
 * 端到端行为（真实 localStorage / fetch / 401 跳登录页）由 /tmp 下的 DOM 桩
 * 脚本执行真实的 api.js + app.js 验证，**不入库** —— 入库的必须是纯逻辑，
 * 否则它依赖一套只有本机有的桩，反而成了「测试跟着实现走」。
 *
 * 守护的四条契约：
 *   A. 未登录 → 业务端点一律 deny（不发任何业务请求）
 *   B. 登录成功 → Authorization: Bearer <token>，且不产生 `Bearer undefined`
 *   C. 401 → 跳回登录页；**403 不是** 401（不能把「无权」当成「没登录」）
 *   D. 令牌缺失 / 过期 / 形状不对 → 停在登录页（fail closed）
 */
'use strict';

const assert = require('assert');
const path = require('path');

const API_JS = path.join(__dirname, '..', '..', 'web', 'static', 'js', 'api.js');
const src = require('fs').readFileSync(API_JS, 'utf8');

const BEGIN = '== BEGIN PURE-AUTH-LOGIC ==';
const END = '== END PURE-AUTH-LOGIC ==';
assert.ok(src.includes(BEGIN), 'api.js 缺少纯逻辑块起始标记');
assert.ok(src.includes(END), 'api.js 缺少纯逻辑块结束标记');
// 标记本身写在 /* */ 里，所以要从标记**所在注释的结尾**之后开始切，
// 否则会把 ` */` 留在代码块开头，求值时变成「注释未闭合」。
function sliceBlock() {
  const afterBegin = src.indexOf(BEGIN) + BEGIN.length;
  const beginClose = src.indexOf('*/', afterBegin);
  const endAt = src.indexOf(END);
  const endClose = src.lastIndexOf('/*', endAt);
  assert.ok(beginClose > afterBegin && beginClose < endAt, '纯逻辑块起始标记的注释未闭合');
  assert.ok(endClose > beginClose, '纯逻辑块结束标记的注释未闭合');
  return src.slice(beginClose + 2, endClose);
}
const block = sliceBlock();

// 间接 eval ⇒ 求值在全局作用域，函数声明不会漏出本模块
(0, eval)(block + '\n;globalThis.__AUTH_LOGIC__ = PAAuthLogic;');
const A = globalThis.__AUTH_LOGIC__;
assert.ok(A, '纯逻辑块必须导出 PAAuthLogic');

/* ── 令牌构造：与 pa_agent/storage/auth.py::issue_token 同形 ────────────────
 * v1.<base64url(payload_json)>.<base64url(sig)>。签名对前端无意义
 * （前端没有密钥，也不该有），这里只保证形状一致。 */
function b64url(obj) {
  return Buffer.from(JSON.stringify(obj), 'utf8')
    .toString('base64')
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '');
}
function makeToken(claims) {
  return `v1.${b64url(claims)}.${b64url({ sig: true })}`;
}
const NOW = 1_700_000_000;
const validToken = makeToken({ sub: 'admin', iat: NOW - 60, exp: NOW + 3600 });
const expiredToken = makeToken({ sub: 'admin', iat: NOW - 7200, exp: NOW - 3600 });

/* ── decodeTokenPayload ─────────────────────────────────────────────────── */
assert.deepStrictEqual(
  A.decodeTokenPayload(validToken),
  { sub: 'admin', iat: NOW - 60, exp: NOW + 3600 },
  'must decode the payload segment of v1.<payload>.<sig>'
);

// base64url 的 `-` / `_` 变体（非 base64 标准字母表）
assert.strictEqual(
  A.decodeTokenPayload(makeToken({ sub: 'a?b>c~d', exp: NOW + 10 })).sub,
  'a?b>c~d',
  'payload must survive base64url encoding (no + or /)'
);

// 形状不对的一律 null，绝不抛
for (const bad of [
  undefined, null, '', '   ', 'not-a-token',
  'v2.aaa.bbb',                    // 版本前缀不符
  'v1.aaa',                       // 段数不足
  'v1.aaa.bbb.ccc',               // 段数过多
  'v1.!!!.bbb',                   // 载荷不是 base64
  'v1.' + Buffer.from('not json').toString('base64url') + '.bbb',
  123,                            // 非字符串
  { a: 1 },
]) {
  assert.strictEqual(A.decodeTokenPayload(bad), null,
    `decodeTokenPayload must return null (never throw) for ${JSON.stringify(bad)}`);
}
// 载荷是合法 JSON 但不是对象（数组/标量）也判无效
assert.strictEqual(A.decodeTokenPayload(`v1.${Buffer.from('[1,2]').toString('base64url')}.x`), null);

/* ── hasToken：能不能发业务请求的唯一判据 ───────────────────────────────── */
assert.strictEqual(A.hasToken(validToken), true);
assert.strictEqual(A.hasToken('  '), false, '空白串不是令牌');
assert.strictEqual(A.hasToken(''), false);
assert.strictEqual(A.hasToken(null), false);
assert.strictEqual(A.hasToken(undefined), false);
assert.strictEqual(A.hasToken(123), false);

/* ── isTokenLocallyExpired：**只**用于「别白跑一趟」的快路径 ───────────── */
assert.strictEqual(A.isTokenLocallyExpired(validToken, NOW), false);
assert.strictEqual(A.isTokenLocallyExpired(expiredToken, NOW), true, '已过期必须本地判死');

// leeway：与后端 verify_token(leeway_s=30) 对齐，客户端时钟略慢不该提前判死
assert.strictEqual(
  A.isTokenLocallyExpired(makeToken({ sub: 'admin', exp: NOW - 10 }), NOW), false,
  'exp 刚过、仍在 leeway 内不算本地过期（对齐后端 leeway_s=30）'
);
assert.strictEqual(
  A.isTokenLocallyExpired(makeToken({ sub: 'admin', exp: NOW - A.LEEWAY_S - 1 }), NOW), true,
  '超出 leeway 必须判死'
);
// leeway 常量必须就是 30：与后端写死值对不上会让两端对「过期」的分叉
assert.strictEqual(A.LEEWAY_S, 30);

/* ⚠️ 判不出 ≠ 已死。方向反了会造成**最坏的一种故障**：一个服务端仍然认可的
 * 令牌被前端锁死，用户被挡在登录页外却说不出为什么。下面每一条都必须 false。 */
for (const undecidable of [
  '', null, undefined, 'garbage',
  makeToken({ sub: 'admin' }),              // 没有 exp
  makeToken({ sub: 'admin', exp: 'soon' }), // exp 不是数字
  makeToken({ sub: 'admin', exp: null }),
  'v2.aaa.bbb',
]) {
  assert.strictEqual(A.isTokenLocallyExpired(undecidable, NOW), false,
    `判不出过期必须 false（不得据此把用户锁在登录页外）：${JSON.stringify(undecidable)}`);
}

/* ── authHeaderValue ────────────────────────────────────────────────────── */
assert.strictEqual(A.authHeaderValue(validToken), `Bearer ${validToken}`);
assert.strictEqual(A.authHeaderValue(`  ${validToken}  `), `Bearer ${validToken}`,
  '令牌两端空白必须去掉，否则后端 bearer_token() 解析出的令牌带空白 → 校验失败');
assert.strictEqual(A.authHeaderValue(''), '');
assert.strictEqual(A.authHeaderValue(null), '');
assert.strictEqual(A.authHeaderValue(undefined), '');
for (const v of ['Bearer undefined', 'Bearer null']) {
  assert.notStrictEqual(A.authHeaderValue(undefined), v,
    '无令牌时绝不能拼出字面量 "Bearer undefined"');
}

/* ── isAuthEndpoint ─────────────────────────────────────────────────────── */
for (const p of ['/api/auth/login', '/api/auth/logout', '/api/auth/me', '/api/auth',
  '/api/auth/login?x=1', '/api/auth/me#frag']) {
  assert.strictEqual(A.isAuthEndpoint(p), true, `${p} 必须豁免鉴权`);
}
for (const p of ['/api/bars', '/api/settings', '/api/analyze/stream', '/api/authors',
  '/api/authlogin', '', null, undefined, 42]) {
  assert.strictEqual(A.isAuthEndpoint(p), false, `${p} 不能豁免鉴权`);
}

/* ── isUnauthorizedStatus ───────────────────────────────────────────────── */
assert.strictEqual(A.isUnauthorizedStatus(401), true);
// 403 = 已登录但无权。清掉令牌会把用户踢回登录页去重试一件他没做错的事。
assert.strictEqual(A.isUnauthorizedStatus(403), false, '403 不等于未登录');
assert.strictEqual(A.isUnauthorizedStatus(400), false);
assert.strictEqual(A.isUnauthorizedStatus(404), false);
assert.strictEqual(A.isUnauthorizedStatus(500), false);
assert.strictEqual(A.isUnauthorizedStatus(undefined), false);

/* ── 契约 A：未登录时业务请求一律不发 ──────────────────────────────────── */
const BUSINESS = [
  '/api/settings', '/api/bars?count=100', '/api/bars/next-close',
  '/api/exchanges', '/api/symbols', '/api/timeframes', '/api/order-types',
  '/api/records', '/api/experience', '/api/health',
  '/api/subscribe', '/api/demo/sample',
  '/api/analyze/stream', '/api/analyze/incremental/stream', '/api/chat/stream',
];
const AUTH = ['/api/auth/login', '/api/auth/logout', '/api/auth/me'];

for (const ep of BUSINESS) {
  assert.strictEqual(A.authDecisionFor(ep, '', NOW), 'deny',
    `契约A：无令牌时 ${ep} 必须被拒（未登录绝不发业务请求）`);
  assert.strictEqual(A.authDecisionFor(ep, null, NOW), 'deny');
  assert.strictEqual(A.authDecisionFor(ep, expiredToken, NOW), 'deny',
    `契约A：本地已判死的令牌不发 ${ep}（省掉一次注定 401 的往返）`);
  assert.strictEqual(A.authDecisionFor(ep, validToken, NOW), 'send',
    `契约A：有效令牌时 ${ep} 必须放行`);
  // 「本地判不出」也必须放行 —— 否则前端会锁死服务端仍认的令牌
  assert.strictEqual(A.authDecisionFor(ep, 'garbage', NOW), 'send',
    `契约A：本地判不出过期时不得拦 ${ep}（判据归服务端）`);
}
for (const ep of AUTH) {
  assert.strictEqual(A.authDecisionFor(ep, '', NOW), 'send',
    `${ep} 在没有令牌时也必须能发出去，否则永远登不上`);
  assert.strictEqual(A.authDecisionFor(ep, expiredToken, NOW), 'send',
    `${ep} 即便令牌已过期也要能发（登出/换登录都要靠它）`);
  assert.strictEqual(A.authDecisionFor(ep, validToken, NOW), 'send');
}

/* ── 契约 C：401 → 跳登录页；登录端点的 401 不算 ─────────────────────────── */
for (const ep of BUSINESS) {
  assert.strictEqual(A.postResponseDecision(ep, 401), 'login',
    `契约C：${ep} 返回 401 必须跳登录页`);
  assert.strictEqual(A.postResponseDecision(ep, 500), 'continue');
  assert.strictEqual(A.postResponseDecision(ep, 200), 'continue');
}
// 输错密码会得到 /api/auth/login 的 401：它必须只提示、不清会话，
// 否则一次手滑就把用户踢出本来有效的登录态。
for (const ep of AUTH) {
  assert.strictEqual(A.postResponseDecision(ep, 401), 'continue',
    `契约C：${ep} 的 401 不能踢会话（登录失败 ≠ 会话失效）`);
  assert.strictEqual(A.postResponseDecision(ep, 403), 'continue');
}

/* ── 契约 D：boot 闸门 ─────────────────────────────────────────────────── */
// 有令牌 → 'probe'（去问 /api/auth/me，**由服务端判过期**）
// 无令牌 → 'login'（一个请求都不发）
for (const t of [validToken, expiredToken, 'garbage', 'v1.zzz.yyy']) {
  assert.strictEqual(A.decideBootGate(t), 'probe',
    '有令牌就必须去问服务端，前端无权自行判死');
}
// 空串 / 纯空白 / null / undefined ⇒ 没有令牌可问，直接停在登录页
for (const t of ['', ' ', '   ', null, undefined, 0, false, {}]) {
  assert.strictEqual(A.decideBootGate(t), 'login',
    `没有令牌必须停在登录页（一个请求都不发）：${JSON.stringify(t)}`);
}

console.log('login_gate: all assertions passed');

/* ── 契约守卫：app.js 调的 PAuth.* 必须在 api.js 里有定义 ──────────────────
 *
 * 2026-10-05 实测事故：一次提交把 api.js 的新版与 app.js 的旧版一起带走，
 * HEAD 内部自相矛盾 —— app.js 调 PAuth.isTokenUsable（api.js 已不导出），
 * 登录提交直接 TypeError；decideBootGate(...) !== 'boot' 恒真则永远停在登录页。
 * 两份文件都能通过 node --check，而浏览器里才发现整个登录是死的。
 *
 * 语法检查抓不到跨文件的「调用了不存在的导出」，所以这条断言必须存在。
 */
const _fs2 = require('fs');
const _path2 = require('path');
const _root2 = _path2.join(__dirname, '..', '..');
const _app2 = _fs2.readFileSync(_path2.join(_root2, 'web/static/js/app.js'), 'utf8');
const _api2 = _fs2.readFileSync(_path2.join(_root2, 'web/static/js/api.js'), 'utf8');

const _called = [...new Set([..._app2.matchAll(/PAuth\.([A-Za-z_]\w*)/g)].map((m) => m[1]))];
const _missing = _called.filter((n) => !new RegExp(`\\b${n}\\b`).test(_api2));
if (_missing.length) {
  console.error(`FAIL app.js 调用了 api.js 未导出的 PAuth 成员: ${_missing.join(', ')}`);
  process.exit(1);
}
console.log(`contract: app.js 的 ${_called.length} 个 PAuth 调用全部有定义`);
