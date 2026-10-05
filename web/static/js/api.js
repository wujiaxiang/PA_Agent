// api.js — PA Agent Web API client

// ── 两种身份，别混 ──────────────────────────────────────────────────────────
//   session_id（X-Session-Id）= **这个标签页是谁**，游标 / 追问分桶的键。
//     存 sessionStorage，克隆检测逻辑见下。
//   auth token（Authorization: Bearer）= **这个用户是谁**，跨标签页、跨刷新。
//     存 localStorage。签发与校验都在后端（pa_agent/storage/auth.py 的
//     `v1.<payload>.<sig>`，web/api/auth_ctx.py 的 `bearer_token()`），
//     前端**不重复实现一套**：只负责存取、加头、判有效期。
//
// ── 会话身份 ────────────────────────────────────────────────────────────────
// session_id 决定「这个请求属于哪个标签页」：游标（symbol/timeframe/exchange）、
// 增量分析锚点、追问分桶键（routes_chat.py 的 `sid|record|symbol|tf|k|n`）
// 全部以它为键。每次请求带 X-Session-Id；缺失或非法时后端回落全局设置
// （行为与改造前一致，web/api/session_ctx.py）。
//
// **存哪**：sessionStorage。它的语义天生是「每标签页独立 + 扛住 F5 刷新」。
// Cookie 与 localStorage 都同源共享 —— 所有标签页拿到同一个 id，
// 「一个标签页一张自己的 K 线图」直接失效。
//
// ── 由此得到的能力：刷新可还原 ────────────────────────────────────────────
// session_id 跨刷新不变 ⇒ 同一个键，后端三处还原全部自动生效：
//   1. `GET /api/settings` 用**本会话游标**回填 general.last_symbol 等
//      （routes_settings.py::_apply_session_cursor → resolve_view：内存热层
//      → SQLite `sessions` 快照，均带 TTL），前端 loadSettings() 据此写
//      `#ds-symbol` / `#ds-exchange` / `#ds-timeframe`；
//   2. 随后的 `GET /api/bars` 同样按本会话游标取数（routes_data.py::
//      _resolve_view），不需要先 POST /api/subscribe；
//   3. 追问命中同一分桶键，FreeChatSession 复用 ⇒ 历史接着聊。
//
// ── 由此带来的代价：复制标签页会克隆 sessionStorage ────────────────────────
// 浏览器「复制标签页」把 sessionStorage **整份克隆**给新标签页，两个 tab 于是
// 拿到同一个 session_id，而上面那三处状态全部以它为键 —— 隔离被打回原形。
//
// **两者只能同时成立，办法是把「身份」与「标签页实例」拆开**：
//   - **身份** session_id：稳定存 sessionStorage（跨刷新不变 ⇒ 可还原）；
//   - **标签页实例** _paInstanceId：**每次页面加载重新生成，只存在于内存**，
//     不写任何 storage；
//   - 用 BroadcastChannel（同源、无依赖）把两者对齐：新页面宣告「我持有 id X」，
//     若别的页面应答「我也持有 X」，自己就是被克隆出来的那一个 → 就地换新 id。
//     首个标签页无人应答 ⇒ 超时后照常放行，绝不卡住（见 _startCloneDetection）。
//
// **残留风险（明确接受）**：应答方若在握手窗口内事件循环被占满（长任务、
// 后台标签页冻结），克隆页会收不到应答而沿用同一个 id —— 退化成「像只有
// sessionStorage 那样共享状态」，即改造前的行为，不会更坏。窗口只有 ~150ms，
// 且复制标签页是用户主动操作、源标签页此刻通常空闲。
//
// **被本文件删除的东西**：改造前追加在 id 尾部的「每页 nonce」（base + '-' +
// nonce = 29 字符）。它只为克隆检测而存在，代价是 F5 必然换 id（游标与追问
// 历史全丢）。注意「同 tab 不同记录的追问隔离」**从来不是**靠它，而是服务端
// session_key 里的 record_key 段（routes_chat.py:362-375），与本文件无关 ——
// 故恢复刷新还原不会削弱它。
const SESSION_ID_KEY = 'pa_agent_session_id';

// ── id 形态（必须与后端 sanitize_session_id 逐字对齐）──────────────────────
// 后端规则（web/api/session_ctx.py）：非空、长度 ≤ 64、字符集 [A-Za-z0-9_-]。
// **不合规的 id 会被静默丢弃**（返回 ''）⇒ 全部请求回落全局游标 ⇒
// 隔离与还原一起消失且无任何报错。故前端生成后自己先校验一遍。
// 'pa-' 前缀 + 32 位十六进制 = **35 字符**（128 bit 随机），远低于 64 上限，
// 前缀让 sessions 表 / 日志里的行一眼可辨。前缀与十六进制都落在允许字符集内。
const SESSION_ID_MAX_LEN = 64;
const SESSION_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;
const SESSION_ID_PREFIX = 'pa-';
const SESSION_ID_HEX_LEN = 32;

// 克隆握手窗口。超时即「没人跟我抢这个 id」⇒ 放行（首个标签页必然走这条路）。
const CLONE_HANDSHAKE_MS = 150;
const PA_CHANNEL_NAME = 'pa_agent_session_v1';

// ── 模块级状态 ──────────────────────────────────────────────────────────────
let _paSessionId = null;      // 身份（最终值；握手期间是候选值）
let _paInstanceId = null;     // 标签页实例：每次页面加载重新生成，仅内存
let _paBornAt = null;         // 本代起始时刻：ack 的仲裁基准（见下方注释）
let _paMemoryId = null;       // sessionStorage 不可用时的纯内存 id
let _paReadySettled = false;
let _paReadyResolve = null;
const _paReadyPromise = new Promise((resolve) => { _paReadyResolve = resolve; });

function _paRandomHex(n) {
  const buf = new Uint8Array(n);
  try {
    if (window.crypto && crypto.getRandomValues) {
      crypto.getRandomValues(buf);
      return Array.from(buf, b => b.toString(16).padStart(2, '0')).join('').slice(0, n);
    }
  } catch (_) { /* 落到 Math.random 兜底 */ }
  let s = '';
  while (s.length < n) s += Math.random().toString(36).slice(2);
  return s.slice(0, n);
}

// 一次页面加载内的稳定随机值（16 字符，base36 ⊂ 允许字符集）。
function pageNonce() {
  if (!_paInstanceId) _paInstanceId = _paRandomHex(16);
  if (!_paBornAt) _paBornAt = Date.now();
  return _paInstanceId;
}

function _paNewSessionId() {
  const id = SESSION_ID_PREFIX + _paRandomHex(SESSION_ID_HEX_LEN);
  // 自查：宁可当场换一个，也绝不把不合规的 id 发出去（会被静默丢弃）。
  return SESSION_ID_RE.test(id) && id.length <= SESSION_ID_MAX_LEN
    ? id
    : _paRandomHex(SESSION_ID_HEX_LEN);
}

// 读出稳定身份。存储里的值必须先过一遍后端同一套规则：非法（被截断、被别的
// 版本写入、被手改）就换一个，否则后端会静默丢弃 → 回落全局游标。
function _readStoredSessionId() {
  let raw = null;
  let readable = true;
  try {
    raw = sessionStorage.getItem(SESSION_ID_KEY);
  } catch (_) {
    readable = false; // 隐私模式 / storage 被禁用
  }
  if (readable && typeof raw === 'string' && SESSION_ID_RE.test(raw) && raw.length <= SESSION_ID_MAX_LEN) {
    return raw;
  }
  const fresh = _paNewSessionId();
  try {
    // 写不进去（配额满 / 只读）不该把刚读到的身份降级成内存态：仍然用它，
    // 只是下一次刷新未必还在 —— 那也比悄悄换成一次性 id 强。
    sessionStorage.setItem(SESSION_ID_KEY, fresh);
    return fresh;
  } catch (_) {
    if (!readable) {
      // 隐私模式 / storage 被禁用：退化成纯内存 id。仍是本页独有的 id（隔离有效），
      // 但刷新必然换 id（还原失效）—— 没有 storage 就无解，不假装能做到。
      if (!_paMemoryId) _paMemoryId = fresh;
      return _paMemoryId;
    }
    return fresh;
  }
}

/** 当前身份（同步）。握手未完成时返回的是**候选值**，仅供内部/调试使用；
 *  任何真正的请求都必须先 `await whenSessionReady()`。 */
function getSessionId() {
  if (!_paSessionId) _paSessionId = _readStoredSessionId();
  return _paSessionId;
}

/** 身份定稿（含克隆检测）。所有发请求的入口都必须先 await 它。 */
function whenSessionReady() {
  return _paReadySettled ? Promise.resolve(_paSessionId) : _paReadyPromise;
}

// ── 克隆检测（BroadcastChannel 握手）──────────────────────────────────────
// 协议（两个报文，无服务端参与）：
//   claim{id,from} —— 「我持有 id」。收到别人对本 id 的 claim → 对方是克隆
//                     （我是先到的那个，继续用这个 id）；
//   ack {id,from}  —— 「我也持有 id」。收到它 → 自己是被克隆出来的，换新 id。
// BroadcastChannel 不会把消息投递回发送方，from 只是额外的保险。
function _startCloneDetection() {
  const initial = getSessionId();
  if (typeof BroadcastChannel !== 'function') return Promise.resolve(initial);
  let ch = null;
  try {
    ch = new BroadcastChannel(PA_CHANNEL_NAME);
  } catch (_) {
    return Promise.resolve(initial); // 建不出来就退化成「不检测克隆」
  }

  return new Promise((resolve) => {
    let rotated = false;

    const rotate = () => {
      if (rotated) return;
      rotated = true;
      const fresh = _paNewSessionId();
      _paSessionId = fresh;
      try {
        sessionStorage.setItem(SESSION_ID_KEY, fresh);
      } catch (_) { /* 纯内存模式，写不进去无所谓 */ }
      // 主动宣告新身份：万一撞上极小概率碰撞，对方能立刻发现并各走各的。
      try { ch.postMessage({ type: 'claim', id: fresh, from: _paInstanceId, bornAt: _paBornAt }); } catch (_) {}
    };

    const reply = (id) => {
      try { ch.postMessage({ type: 'ack', id, from: _paInstanceId, bornAt: _paBornAt }); } catch (_) {}
    };

    ch.onmessage = (ev) => {
      const m = ev && ev.data;
      if (!m || typeof m.id !== 'string' || m.from === _paInstanceId) return;
      if (m.id !== _paSessionId) return;      // 别人的 id：与我无关
      if (m.type === 'claim') {
        rotate();                              // 对方在宣告同一个 id ⇒ 我是克隆
      } else if (m.type === 'ack') {
        rotate();                              // 有人应答我的 id ⇒ 我是克隆
      }
    };

    try { ch.postMessage({ type: 'claim', id: initial, from: _paInstanceId, bornAt: _paBornAt }); } catch (_) {}

    setTimeout(() => {
      // 超时 = 没人跟我抢这个 id（首个标签页必然如此）⇒ 放行，**不卡住**。
      // 之后仍保持应答能力：后来被复制的标签页要靠我 ack 才能发现自己被克隆。
      _paReadySettled = true;
      ch.onmessage = (ev) => {
        const m = ev && ev.data;
        if (!m || typeof m.id !== 'string' || m.from === _paInstanceId) return;
        if (m.type === 'claim') {
          if (m.id === _paSessionId) reply(m.id);
          return;
        }
        // **窗口外仍要认 ack**。原实现把 onmessage 整个换成只处理 claim 的版本，
        // 于是 ack 被丢弃：克隆页若在窗口外才启动，就收不到「别人也持有这个 id」
        // 的信号，两边顶着同一个 id 各看各的 —— 隔离失效且毫无报错。
        if (m.type === 'ack' && m.id === _paSessionId) {
          // bornAt 仲裁：只认「我这一代」之后的宣战。上一代残留的 ack 到达时
          // 我已经轮换过 id，上面那行 m.id !== _paSessionId 自然挡住；这里再挡
          // 一次是为了「我轮换后又被切回旧 id」这种极端时序。
          if (typeof m.bornAt === 'number' && m.bornAt < _paBornAt) return;
          rotate();
        }
      };
      resolve(_paSessionId);
    }, CLONE_HANDSHAKE_MS);
  });
}

// 启动身份协商。api.js 一加载就跑，早于 app.js 的任何 DOMContentLoaded 请求。
// 全程 try/except：任何异常都必须降级成「用当前候选 id 正常发请求」，
// 绝不能让会话协商把整个页面拖死。
function _bootstrapSessionIdentity() {
  pageNonce();
  getSessionId();
  try {
    const done = _startCloneDetection();
    Promise.resolve(done).then((sid) => {
      if (sid) _paSessionId = sid;
      _paReadySettled = true;
      if (_paReadyResolve) _paReadyResolve(_paSessionId);
    }, () => {
      _paReadySettled = true;
      if (_paReadyResolve) _paReadyResolve(_paSessionId);
    });
  } catch (_) {
    _paReadySettled = true;
    if (_paReadyResolve) _paReadyResolve(_paSessionId);
  }
}
_bootstrapSessionIdentity();

/** 丢掉当前 session_id（登出时用）。
 *
 * **为什么要连身份一起丢**：游标（symbol/timeframe/exchange）、增量分析锚点、
 * 追问历史全都以 session_id 为键。换了用户却不换 id，新用户会继承上一个用户
 * 留在同一 id 下的游标与追问线程。重建一个 id 比事后排查「怎么一登录就跳到
 * 别的品种」便宜得多。
 *
 * 调用方负责随后 reload —— 本函数只管把身份与内存态清干净。
 */
function resetSessionId() {
  _paSessionId = null;
  _paInstanceId = null;
  _paBornAt = null;
  _paMemoryId = null;
  try {
    sessionStorage.removeItem(SESSION_ID_KEY);
  } catch (_) { /* 无 storage 可清 */ }
}

// 同步版本保留给内部/调试；**发请求前请用 sessionHeadersAsync()**。
//
// **两个身份头都在这里汇合**（X-Session-Id + Authorization）：五个入口全部经过
// 它，新增入口自动带令牌。早期版本给每个入口各写一遍 setHeader，漏一个就是
// 一个「静默不带令牌」的接口，而且不报错。
function sessionHeaders(extra = {}) {
  const h = extra ? { ...extra } : {};
  const sid = getSessionId();
  if (sid) h['X-Session-Id'] = sid;
  // 令牌**绝不进 URL**（query / path）：URL 会进 access log、Referer、浏览器
  // 历史，泄漏面比头字段大一个量级。withSessionQuery 只拼 sid。
  const auth = authHeaderValue(readAuthToken());
  if (auth) h['Authorization'] = auth;
  return h;
}

async function sessionHeadersAsync(extra = {}) {
  await whenSessionReady();
  return sessionHeaders(extra);
}

// SSE 端点双通道兜底：API.sse 走 fetch，请求头已经带了 X-Session-Id；这里再
// 把 ?sid= 拼进 URL —— 反向代理/网关剥掉自定义头是常见部署坑，而
// session_id_of(request) 的 query 分支就是为它准备的（原生 EventSource 带不了
// 头，前端若改回 EventSource 也仍能区分会话）。
function withSessionQuery(url) {
  const sid = getSessionId();
  if (!sid) return url;
  const sep = url.includes('?') ? '&' : '?';
  return `${url}${sep}sid=${encodeURIComponent(sid)}`;
}

/* == BEGIN PURE-AUTH-LOGIC == */
// ── 鉴权纯逻辑（本段必须自包含：无 DOM / 无 storage / 只依赖内建 + atob）────
// tests/js/test_login_gate.test.js 会把两个 BEGIN/END 标记之间**单独求值**做
// 纯逻辑回归。在里面引入任何依赖 window/storage 的东西，那条测试就废了。
const AUTH_TOKEN_PREFIX = 'v1';
/** 与后端 verify_token(leeway_s=30) 对齐：客户端时钟略慢时不该提前判死。 */
const AUTH_LEEWAY_S = 30;

/** base64url → 文本。
 *
 * **必须先把 url-safe 字母表换回标准的**：`atob` 只认 `+ /`，
 * 遇到 `-` / `_` 直接抛 "Invalid character" —— 而这两个字符在真实载荷里
 * 并不罕见（JSON 文本的 base64 编码随时会产出）。少了这一步，短用户名
 * （'admin' 恰好编不出 `-`/`_`）能过，长一点的 sub 就会静默返回 null。
 * padding 同样要自己补，atob 不接受缺省长度的串。 */
function _b64UrlToText(seg) {
  if (typeof seg !== 'string' || !seg) return null;
  const std = seg.replace(/-/g, '+').replace(/_/g, '/');
  const pad = '='.repeat((4 - (std.length % 4)) % 4);
  try {
    return decodeURIComponent(
      Array.from(atob(std + pad), (ch) =>
        '%' + ch.charCodeAt(0).toString(16).padStart(2, '0')
      ).join('')
    );
  } catch (_) {
    return null;
  }
}

/** 解析 `v1.<payload>.<sig>` 的载荷。**不验签**（前端没有密钥，也不需要）。
 *  这里只用来「本地预判令牌是否还有效」，避免拿一个必然 401 的令牌去打接口。 */
function decodeTokenPayload(token) {
  if (typeof token !== 'string') return null;
  const parts = token.trim().split('.');
  if (parts.length !== 3 || parts[0] !== AUTH_TOKEN_PREFIX) return null;
  const text = _b64UrlToText(parts[1]);
  if (!text) return null;
  try {
    const obj = JSON.parse(text);
    // 数组也是 `typeof === 'object'`，必须显式排掉：载荷只可能是一个对象。
    return obj && typeof obj === 'object' && !Array.isArray(obj) ? obj : null;
  } catch (_) {
    return null;
  }
}

/** 手里有没有一个可以拿去问服务端的令牌。**这是「能不能发业务请求」的唯一
 * 权威判据**：令牌的真实性只能由服务端说（web/api/auth_ctx.py 的
 *  bearer_token() / verify_token()），前端无权也不需要判定。 */
function hasToken(token) {
  return typeof token === 'string' && token.trim() !== '';
}

/** 令牌**本地可判定地**已过期吗 —— 只用于「不要白跑一趟」的快路径。
 *
 * ⚠️ 解不出 / 没有 exp 时返回 **false**（判不出 ≠ 已死）。方向必须是这样：
 * 若把「判不出」当成「已死」，一个服务端仍然认可的令牌会被前端锁死，用户被
 * 挡在登录页外却说不出为什么 —— 那是最坏的一种故障。反过来只是多打一次请求、
 * 吃一个 401 然后回登录页，代价小得多。
 *
 * leeway 与后端 verify_token(leeway_s=30) 对齐，避免两端对「过期」认知分叉。 */
function isTokenLocallyExpired(token, nowSec) {
  const p = decodeTokenPayload(token);
  if (!p) return false;
  if (typeof p.exp !== 'number' || !isFinite(p.exp)) return false;
  const now = typeof nowSec === 'number' ? nowSec : Date.now() / 1000;
  return now >= p.exp + AUTH_LEEWAY_S;
}

/** `Authorization` 头的值；无令牌返回空串（**绝不返回 `Bearer undefined`**）。 */
function authHeaderValue(token) {
  const t = typeof token === 'string' ? token.trim() : '';
  return t ? `Bearer ${t}` : '';
}

/** 免令牌端点：登录本身、登出、身份回显。判据是路径前缀 `/api/auth/`。 */
function isAuthEndpoint(path) {
  if (typeof path !== 'string' || !path) return false;
  const p = path.split('?')[0].split('#')[0];
  return p === '/api/auth' || p.startsWith('/api/auth/');
}

/** **只有 401 意味着「该重新登录」**。403 是「已登录但无权」，把令牌清掉
 *  会把用户踢回登录页去重试一件他根本没做错的事。 */
function isUnauthorizedStatus(status) {
  return status === 401;
}

/** 启动闸门：没有令牌 ⇒ 直接停在登录页（**一个请求都不发**）；
 *  有令牌 ⇒ 去问服务端 `GET /api/auth/me`，**由它说了算**。
 *
 *  不在本地判「过期」是刻意的：web/api/routes_auth.py 的前端契约第 1 条
 *  明确写着「不要自己判断 token 是否过期」。本地判死一个服务端仍认的令牌 =
 *  把用户锁在门外；本地多判活一次 = 多吃一个 401，代价小得多。 */
function decideBootGate(token) {
  return hasToken(token) ? 'probe' : 'login';
}

/** **发请求前的最终裁决**：`'send'` 还是 `'deny'`。
 *  `_authGate()` 只做「读 storage + 按这个结果抛不抛错」的薄包装 ——
 *  策略本身在这里，因此可以被 tests/js/test_login_gate.test.js 纯逻辑覆盖。
 *  这条正是「未登录时不发任何业务请求」的唯一判据。 */
function authDecisionFor(endpoint, token, nowSec) {
  if (isAuthEndpoint(endpoint)) return 'send';   // 登录/登出/me 自己不需要令牌
  if (!hasToken(token)) return 'deny';          // 没令牌 ⇒ 注定 401，不值得发
  if (isTokenLocallyExpired(token, nowSec)) return 'deny';  // 快路径：已死的不发
  return 'send';
}

/** **收到响应后的裁决**：`'continue'` 还是 `'login'`。
 *  登录端点自己的 401 是「密码错了」，必须排除，否则一次输错密码就会
 *  把整个会话踢掉（更糟的是清掉一个其实有效的令牌）。 */
function postResponseDecision(endpoint, status) {
  if (isAuthEndpoint(endpoint)) return 'continue';
  return isUnauthorizedStatus(status) ? 'login' : 'continue';
}

const PAAuthLogic = {
  TOKEN_PREFIX: AUTH_TOKEN_PREFIX,
  LEEWAY_S: AUTH_LEEWAY_S,
  decodeTokenPayload,
  hasToken,
  isTokenLocallyExpired,
  authHeaderValue,
  isAuthEndpoint,
  isUnauthorizedStatus,
  decideBootGate,
  authDecisionFor,
  postResponseDecision,
};
/* == END PURE-AUTH-LOGIC == */

// ── 令牌存取（localStorage）─────────────────────────────────────────────────
// **为什么必须是 localStorage 而不是 sessionStorage**：sessionStorage 活不过
// 刷新。放进去 ⇒ 用户每按一次 F5 就被踢回登录页，与本项目「刷新可还原」
// （游标回填 / 追问续聊）的既有能力直接冲突。
//
// **只存 token，不存密码**：密码每次现输现校验，用完即弃。localStorage 里的
// 密码 = XSS 一发即得长期凭证，而 token 至少还有 exp 这道闸。
// ⚠️ 键名与 web/api/routes_auth.py 的前端契约第 2/4 条逐字对齐（`pa_token`）。
// 对不上不会报错，只会让服务端每次都认为「没有令牌」⇒ 永远停在登录页。
const AUTH_TOKEN_KEY = 'pa_token';

// undefined = 还没从 storage 读过（内存镜像未初始化），'' = 确认没有
let _authToken;
let _authPayload = null;
let _authPayloadFor = '';
let _authHandler = null;
let _authNotified = false;

function _authPayloadOf(token) {
  if (token !== _authPayloadFor) {
    _authPayloadFor = token;
    _authPayload = decodeTokenPayload(token);
  }
  return _authPayload;
}

function readAuthToken() {
  if (_authToken !== undefined) return _authToken;
  let raw = null;
  try {
    raw = localStorage.getItem(AUTH_TOKEN_KEY);
  } catch (_) {
    raw = null; // 隐私模式 / storage 被禁用：按「无令牌」处理，退回登录页
  }
  _authToken = typeof raw === 'string' ? raw.trim() : '';
  return _authToken;
}

function setAuthToken(token) {
  const t = typeof token === 'string' ? token.trim() : '';
  _authToken = t;
  _authPayloadFor = '\u0000never'; // 强制下次重新解码
  _authNotified = false;
  try {
    if (t) localStorage.setItem(AUTH_TOKEN_KEY, t);
    else localStorage.removeItem(AUTH_TOKEN_KEY);
  } catch (_) {
    // 写不进去：内存里仍然有效（本页不会误踢），只是刷新后要重新登录。
  }
  return t;
}

function clearAuthToken() {
  _authToken = '';
  _authPayload = null;
  _authPayloadFor = '';
  try {
    localStorage.removeItem(AUTH_TOKEN_KEY);
  } catch (_) { /* 无 storage 可清 */ }
}

/** 当前 user_id（= 令牌载荷的 `sub`）。拿不到就是空串，不猜。 */
function currentUserId() {
  const p = _authPayloadOf(readAuthToken());
  return p && typeof p.sub === 'string' ? p.sub : '';
}

/** app.js 注册「会话失效」的回调（切登录页）。只保留一个 —— 多注册者只会
 *  让同一次 401 弹好几个登录页。 */
function setUnauthorizedHandler(fn) {
  _authHandler = typeof fn === 'function' ? fn : null;
}

/** 触发一次会话失效处理。**幂等**：boot 期间并发的一批请求会同时拿到 401，
 *  不做这个去重就会切 N 次登录页。 */
function notifyUnauthorized(reason) {
  // 令牌已不可用（缺失 / 本地过期 / 服务端拒绝）⇒ 本地立即作废，
  // 后续请求由闸门直接拦下，不会继续打接口。
  clearAuthToken();
  if (_authNotified || !_authHandler) return;
  _authNotified = true;
  try {
    _authHandler(reason);
  } catch (_) { /* 处理失败也不许连累调用方 */ }
}

/** 发请求前的闸门。返回 null 表示放行，返回 Error 表示就地拒绝。 */
function _authGate(endpoint) {
  const token = readAuthToken();
  // 策略在纯逻辑块里（authDecisionFor），这里只负责取 storage + 落地后果。
  if (authDecisionFor(endpoint, token) !== 'deny') return null;
  notifyUnauthorized(hasToken(token) ? 'expired' : 'missing');
  return new Error('需要登录');
}

/** 响应侧的闸门：只有**业务端点**的 401 才踢回登录页。登录接口自己的 401
 *  是「密码错了」，交给登录界面提示，不该顺手清掉一个有效的会话。 */
function _authGateResponse(endpoint, response) {
  if (!response) return;
  if (postResponseDecision(endpoint, response.status) === 'login') {
    notifyUnauthorized('rejected');
  }
}

const PAuth = {
  TOKEN_KEY: AUTH_TOKEN_KEY,
  /** 会话已被作废。app.js 用它抑制 toast：登录页盖住主界面后，
   *  在途请求的失败回调再弹一片错误提示只会更糟。 */
  sessionDead: false,
  readAuthToken,
  setAuthToken,
  clearAuthToken,
  currentUserId,
  setUnauthorizedHandler,
  decodeTokenPayload,
  hasToken,
  isTokenLocallyExpired,
  authHeaderValue,
  isAuthEndpoint,
  isUnauthorizedStatus,
  decideBootGate,
  resetSessionId,
};

const API = {
  // **每个发请求的入口都必须先 await sessionHeadersAsync()**：
  // 身份在 ~150ms 的克隆握手后才定稿。克隆出来的标签页若带着被克隆的 id 先发
  // POST /api/subscribe，就会把**原标签页**的游标覆盖掉 —— 隔离与还原一起坏。
  //
  // **鉴权是第二道闸**：app.js 在 boot 最前面挡掉未登录用户，但那只管「进页面
  // 的那一刻」。用户登出后 / 多标签页 / 令牌中途过期时，界面上仍可能有点击、
  // 有轮询在跑。这里就地拒绝，比让请求打出去吃 401 再补救可靠。
  async get(endpoint) {
    const denied = _authGate(endpoint);
    if (denied) throw denied;
    const headers = await sessionHeadersAsync();
    const r = await fetch(endpoint, { cache: 'no-cache', headers });
    _authGateResponse(endpoint, r);
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  },

  async put(endpoint, body) {
    const denied = _authGate(endpoint);
    if (denied) throw denied;
    const r = await fetch(endpoint, {
      method: 'PUT',
      headers: await sessionHeadersAsync({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body),
    });
    _authGateResponse(endpoint, r);
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  },

  // options.timeout：请求超时（毫秒），默认 15000。超时后通过 AbortController
  // 取消 fetch，并抛出 Error('timeout')，调用方可在 catch 中识别 e.message === 'timeout'
  async post(endpoint, body, options = {}) {
    // 闸门必须在建 AbortController / 起定时器**之前**：被拒绝时不该留下一个
    // 没人清的 setTimeout，更不该先 await 一次身份握手。
    const denied = _authGate(endpoint);
    if (denied) throw denied;
    const opts = {
      method: 'POST',
      // 超时计时从身份定稿之后才开始算，避免握手耗时挤占请求预算。
      headers: await sessionHeadersAsync({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body),
    };
    const timeoutMs = options.timeout || 15000;
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const r = await fetch(endpoint, { ...opts, signal: controller.signal });
      clearTimeout(timeoutId);
      _authGateResponse(endpoint, r);
      // 尝试解析后端返回的结构化错误（含 error_type 字段）
      if (!r.ok) {
        const text = await r.text();
        let err;
        try {
          const j = JSON.parse(text);
          err = new Error(j.detail || text);
          if (j.error_type) err.error_type = j.error_type;
        } catch (_) {
          err = new Error(text);
        }
        // 附加 X-Error-Type 响应头（后端 routes_data.py 设置）
        const hdrErrType = r.headers.get('X-Error-Type');
        if (hdrErrType && !err.error_type) err.error_type = hdrErrType;
        throw err;
      }
      return r.json();
    } catch (e) {
      clearTimeout(timeoutId);
      // AbortController 触发的 abort 会抛出 AbortError，统一转换成 'timeout'
      if (e.name === 'AbortError') throw new Error('timeout');
      throw e;
    }
  },

  async delete(endpoint) {
    const denied = _authGate(endpoint);
    if (denied) throw denied;
    const r = await fetch(endpoint, { method: 'DELETE', headers: await sessionHeadersAsync() });
    _authGateResponse(endpoint, r);
    if (!r.ok) {
      const err = new Error(await r.text());
      err.status = r.status;
      throw err;
    }
    return r.json();
  },

  // SSE helper: returns an AbortController + async generator
  // 走 fetch 而非原生 EventSource：EventSource 无法设置请求头，会话 id 会被
  // 剥掉并静默回落到全局游标。URL 上再挂一个 ?sid= 作为第二通道。
  //
  // 本函数**同步**返回（调用方拿 controller 立刻能 abort），因此**身份等待与
  // 鉴权闸门都放在生成器体内**：URL 与请求头在第一个 next() 时才拼，此时 sid
  // 已定稿、令牌也已校验。放在函数体外就要引入 await，sse() 立刻返回
  // Promise，调用方的 `const { controller } = API.sse(...)` 会拿到 undefined，
  // 停止按钮直接失效（这是既有硬约束，勿破坏）。
  sse(endpoint) {
    const controller = new AbortController();
    const source = (async function* () {
      await whenSessionReady();
      if (controller.signal.aborted) {
        const e = new Error('Aborted');
        e.name = 'AbortError';
        throw e;
      }
      const denied = _authGate(endpoint);
      if (denied) throw denied;
      const url = withSessionQuery(endpoint);
      const r = await fetch(url, {
        signal: controller.signal,
        headers: sessionHeaders(),
      });
      _authGateResponse(endpoint, r);
      if (!r.ok) throw new Error(await r.text());
      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split('\n');
        buf = lines.pop() || '';
        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try { yield JSON.parse(line.slice(6)); } catch (_) {}
          }
        }
      }
    })();
    return { controller, source };
  },
};
