// api.js — PA Agent Web API client

// ── 会话身份 ────────────────────────────────────────────────────────────────
// 每个标签页一个独立会话：UUID 存 sessionStorage，随每次请求带上 X-Session-Id。
//
// 为什么必须是 sessionStorage 而不是 Cookie：Cookie 同源共享，同一浏览器的
// 所有标签页拿到同一个 id，「一个标签页服务自己的 K 线图」直接失效。
// sessionStorage 的语义天生是「每标签页独立」，且能扛住 F5 刷新。
//
// 后端据此隔离游标与增量分析锚点（见 docs/SESSION_STORAGE_DESIGN.md §3）。
// 缺失或非法时后端回落到全局设置，行为与改造前一致。
const SESSION_ID_KEY = 'pa_agent_session_id';

function getSessionId() {
  try {
    let id = sessionStorage.getItem(SESSION_ID_KEY);
    if (!id) {
      id = (crypto.randomUUID ? crypto.randomUUID()
                             : 'sid-' + Date.now() + '-' + Math.random().toString(36).slice(2));
      sessionStorage.setItem(SESSION_ID_KEY, id);
    }
    return id;
  } catch (_) {
    // 隐私模式 / 禁用 storage：返回空串，后端按「无会话」回落
    return '';
  }
}

function sessionHeaders(extra = {}) {
  const sid = getSessionId();
  return sid ? { ...extra, 'X-Session-Id': sid } : extra;
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
  sse(endpoint) {
    const controller = new AbortController();
    const source = (async function* () {
      const r = await fetch(endpoint, {
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
