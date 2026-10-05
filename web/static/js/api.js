// api.js — PA Agent Web API client

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

// 同步版本保留给内部/调试；**发请求前请用 sessionHeadersAsync()**。
function sessionHeaders(extra = {}) {
  const sid = getSessionId();
  return sid ? { ...extra, 'X-Session-Id': sid } : extra;
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

const API = {
  // **每个发请求的入口都必须先 await sessionHeadersAsync()**：
  // 身份在 ~150ms 的克隆握手后才定稿。克隆出来的标签页若带着被克隆的 id 先发
  // POST /api/subscribe，就会把**原标签页**的游标覆盖掉 —— 隔离与还原一起坏。
  async get(endpoint) {
    const headers = await sessionHeadersAsync();
    const r = await fetch(endpoint, { cache: 'no-cache', headers });
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  },

  async put(endpoint, body) {
    const r = await fetch(endpoint, {
      method: 'PUT',
      headers: await sessionHeadersAsync({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  },

  // options.timeout：请求超时（毫秒），默认 15000。超时后通过 AbortController
  // 取消 fetch，并抛出 Error('timeout')，调用方可在 catch 中识别 e.message === 'timeout'
  async post(endpoint, body, options = {}) {
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
    const r = await fetch(endpoint, { method: 'DELETE', headers: await sessionHeadersAsync() });
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
  // 本函数**同步**返回（调用方拿 controller 立刻能 abort），因此身份等待放在
  // 生成器体内：URL 与请求头在第一个 next() 时才拼，此时 sid 已定稿。
  sse(endpoint) {
    const controller = new AbortController();
    const source = (async function* () {
      await whenSessionReady();
      if (controller.signal.aborted) {
        const e = new Error('Aborted');
        e.name = 'AbortError';
        throw e;
      }
      const url = withSessionQuery(endpoint);
      const r = await fetch(url, {
        signal: controller.signal,
        headers: sessionHeaders(),
      });
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
