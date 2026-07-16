// api.js — PA Agent Web API client

const API = {
  async get(endpoint) {
    const r = await fetch(endpoint);
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  },

  async put(endpoint, body) {
    const r = await fetch(endpoint, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  },

  async post(endpoint, body) {
    const r = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  },

  // SSE helper: returns an AbortController + async generator
  sse(endpoint) {
    const controller = new AbortController();
    const source = (async function* () {
      const r = await fetch(endpoint, { signal: controller.signal });
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
