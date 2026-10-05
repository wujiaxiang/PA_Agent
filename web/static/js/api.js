// api.js — PA Agent Web API client

// ── 会话身份 ────────────────────────────────────────────────────────────────
// 每个标签页一个独立会话：UUID 存 sessionStorage，随每次请求带上 X-Session-Id。
//
// 为什么必须是 sessionStorage 而不是 Cookie：Cookie 同源共享，同一浏览器的
// 所有标签页拿到同一个 id，「一个标签页服务自己的 K 线图」直接失效。
// sessionStorage 的语义天生是「每标签页独立」，且能扛住 F5 刷新。
//
// 后端据此隔离游标与增量分析锚点（见 docs/SESSION_STORAGE_DESIGN.md §3），
// 追问会话（/api/chat/stream）也按它分桶。缺失或非法时后端回落到全局设置，
// 行为与改造前一致。
const SESSION_ID_KEY = 'pa_agent_session_id';

// ── 页面实例 nonce ──────────────────────────────────────────────────────────
// **为什么 sessionStorage 里的 UUID 还不够**：浏览器「复制标签页」会把
// sessionStorage **整份克隆**给新标签页，于是两个 tab 拿到**同一个**
// session_id —— 而游标、追问历史、_last_record 全部以 session_id 为键，
// 复制出来的 tab 会与原 tab 共享同一份状态，恰好把「多标签页彻底隔离」打回
// 原形。localStorage 更糟（同源全局共享）；BroadcastChannel 无法区分
// 「F5 刷新」与「复制标签页」。
//
// 因此追加一段**每次页面加载重新生成**的 nonce，且**只存在于内存**（模块级
// 变量，不写任何 storage）：复制标签页会产生新的 JS 上下文 → 新 nonce →
// 新 session_id → 后端分到不同的追问线程。
//
// 代价（明确接受）：F5 刷新后 session_id 也变了，游标与追问历史不保留，
// 回落到全局 settings。单标签页部署无感；多标签页刷新后需重新选一次品种。
const PA_NONCE_LEN = 12;
let _paPageNonce = null;

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

function pageNonce() {
  if (!_paPageNonce) _paPageNonce = _paRandomHex(PA_NONCE_LEN);
  return _paPageNonce;
}

function getSessionId() {
  // 后端 sanitize_session_id 只允许 [A-Za-z0-9_-] 且长度 ≤ 64：
  // base(16) + '-' + nonce(12) = 29 字符，留足余量。超长会被**静默丢弃**，
  // 请求全部回落全局设置 —— 那样隔离等于没做。
  try {
    let base = sessionStorage.getItem(SESSION_ID_KEY);
    if (!base) {
      base = _paRandomHex(16);
      sessionStorage.setItem(SESSION_ID_KEY, base);
    }
    return `${base}-${pageNonce()}`;
  } catch (_) {
    // 隐私模式 / 禁用 storage：退化成「纯 nonce」——仍是本页面独有的 id，
    // 比返回空串（服务端回落全局、彻底不隔离）好。
    return pageNonce();
  }
}

function sessionHeaders(extra = {}) {
  const sid = getSessionId();
  return sid ? { ...extra, 'X-Session-Id': sid } : extra;
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
  async get(endpoint) {
    const r = await fetch(endpoint, { cache: 'no-cache', headers: sessionHeaders() });
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  },

  async put(endpoint, body) {
    const r = await fetch(endpoint, {
      method: 'PUT',
      headers: sessionHeaders({ 'Content-Type': 'application/json' }),
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
      headers: sessionHeaders({ 'Content-Type': 'application/json' }),
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
    const r = await fetch(endpoint, { method: 'DELETE', headers: sessionHeaders() });
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
  sse(endpoint) {
    const controller = new AbortController();
    const url = withSessionQuery(endpoint);
    const source = (async function* () {
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
