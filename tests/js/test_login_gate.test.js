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

/* ── isTokenUsable（fail closed）────────────────────────────────────────── */
assert.strictEqual(A.isTokenUsable(validToken, NOW), true);
assert.strictEqual(A.isTokenUsable(expiredToken, NOW), false, 'exp 已过必须判不可用');
assert.strictEqual(A.isTokenUsable('', NOW), false, '空令牌不可用');
assert.strictEqual(A.isTokenUsable(null, NOW), false);
assert.strictEqual(A.isTokenUsable(undefined, NOW), false);
assert.strictEqual(A.isTokenUsable('garbage', NOW), false);

// 缺 sub / 缺 exp / exp 非数字 —— 一律不可用。
// 「缺 exp 仍放行」看似宽容，实际后果是拿一个本地无法判断的令牌去打接口，
// 靠一串 401 才发现会话早就死了：那正是闸门要消灭的现象。
assert.strictEqual(A.isTokenUsable(makeToken({ exp: NOW + 3600 }), NOW), false, '缺 sub 必须不可用');
assert.strictEqual(A.isTokenUsable(makeToken({ sub: 'admin' }), NOW), false, '缺 exp 必须不可用');
assert.strictEqual(A.isTokenUsable(makeToken({ sub: '', exp: NOW + 3600 }), NOW), false, '空 sub 必须不可用');
assert.strictEqual(A.isTokenUsable(makeToken({ sub: 'admin', exp: 'soon' }), NOW), false);
assert.strictEqual(A.isTokenUsable(makeToken({ sub: 'admin', exp: Infinity }), NOW), false);

// leeway：与后端 verify_token(leeway_s=30) 对齐，客户端时钟略慢不该提前判死
assert.strictEqual(
  A.isTokenUsable(makeToken({ sub: 'admin', exp: NOW - 10 }), NOW), true,
  'exp 刚过、仍在 leeway 内必须放行（对齐后端 leeway_s=30）'
);
assert.strictEqual(
  A.isTokenUsable(makeToken({ sub: 'admin', exp: NOW - A.LEEWAY_S - 1 }), NOW), false,
  '超出 leeway 必须判不可用'
);
// leeway 常量必须就是 30：与后端写死值对不上会让两端对「过期」的认知分叉
assert.strictEqual(A.LEEWAY_S, 30);

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
  '/api/subscribe', '/api/demo/start',
  '/api/analyze/stream', '/api/analyze/incremental/stream', '/api/chat/stream',
];
const AUTH = ['/api/auth/login', '/api/auth/logout', '/api/auth/me'];

for (const ep of BUSINESS) {
  assert.strictEqual(A.authDecisionFor(ep, '', NOW), 'deny',
    `契约A：无令牌时 ${ep} 必须被拒（未登录绝不发业务请求）`);
  assert.strictEqual(A.authDecisionFor(ep, expiredToken, NOW), 'deny',
    `契约A：过期令牌时 ${ep} 必须被拒`);
  assert.strictEqual(A.authDecisionFor(ep, validToken, NOW), 'send',
    `契约A：有效令牌时 ${ep} 必须放行`);
}
for (const ep of AUTH) {
  assert.strictEqual(A.authDecisionFor(ep, '', NOW), 'send',
    `${ep} 在没有令牌时也必须能发出去，否则永远登不上`);
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
assert.strictEqual(A.decideBootGate(validToken, NOW), 'boot');
assert.strictEqual(A.decideBootGate(expiredToken, NOW), 'login');
assert.strictEqual(A.decideBootGate('', NOW), 'login');
assert.strictEqual(A.decideBootGate(null, NOW), 'login');
assert.strictEqual(A.decideBootGate(undefined, NOW), 'login');
assert.strictEqual(A.decideBootGate('v1.zzz.yyy', NOW), 'login');
// 令牌形状被改坏（被别的版本写入、被手改）必须停在登录页，而不是
// 「先试试看」——那是拿一个必然 401 的令牌去污染服务端日志。
assert.strictEqual(A.decideBootGate(makeToken({ sub: 'admin' }), NOW), 'login');

console.log('login_gate: all assertions passed');