/**
 * @platform/sdk — client for the AI Platform.
 *
 * Works in both browser and Node (Node 18+ has global fetch). All auth is via
 * the workspace-scoped API key you got from the dashboard; the SDK adds it as
 * an x-api-key header on every request.
 *
 * Usage:
 *   import { PlatformClient } from "@platform/sdk";
 *   const client = new PlatformClient({ apiKey: "sk_...", baseUrl: "https://..." });
 *   const { reply } = await client.chat("Hello!");
 *   await client.chatStream("Tell me a joke", { onToken: t => process.stdout.write(t) });
 */

export class PlatformClient {
  constructor(opts) {
    if (!opts || !opts.apiKey || !String(opts.apiKey).startsWith("sk_")) {
      throw new Error("PlatformClient: apiKey (sk_...) is required");
    }
    this.apiKey = opts.apiKey;
    this.baseUrl = (opts.baseUrl || "").replace(/\/+$/, "") || (typeof location !== "undefined" ? location.origin : "");
    if (!this.baseUrl) throw new Error("PlatformClient: baseUrl is required in Node");
    this.userId = opts.userId || "";
    this._fetch = opts.fetchImpl || (typeof fetch !== "undefined" ? fetch.bind(globalThis) : null);
    if (!this._fetch) throw new Error("PlatformClient: no fetch available; pass fetchImpl");
  }

  _headers(extra) {
    return Object.assign({ "x-api-key": this.apiKey }, extra || {});
  }

  async _json(method, path, body) {
    const res = await this._fetch(this.baseUrl + path, {
      method,
      headers: this._headers(body !== undefined ? { "content-type": "application/json" } : {}),
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = { error: text }; }
    if (!res.ok) throw new PlatformError((data && (data.error || data.detail)) || res.statusText, res.status);
    return data;
  }

  // ---- workspace / branding ----------------------------------------------

  workspace() { return this._json("GET", "/v1/workspace"); }

  // ---- chat --------------------------------------------------------------

  chat(message, opts = {}) {
    return this._json("POST", "/v1/chat", {
      message,
      conversation_id: opts.conversationId,
      user_id: opts.userId || this.userId || undefined,
    });
  }

  /** Server-Sent Events streaming. Resolves when the stream ends. */
  async chatStream(message, opts = {}) {
    const res = await this._fetch(this.baseUrl + "/v1/chat/stream", {
      method: "POST",
      headers: this._headers({ "content-type": "application/json" }),
      body: JSON.stringify({
        message,
        conversation_id: opts.conversationId,
        user_id: opts.userId || this.userId || undefined,
      }),
    });
    if (!res.ok || !res.body) {
      const t = await res.text();
      const err = new PlatformError(t || res.statusText, res.status);
      if (opts.onError) opts.onError(err.message);
      throw err;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const evt = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const type = (evt.match(/^event:\s*(.+)$/m) || [, ""])[1].trim();
        const data = (evt.match(/^data:\s*(.+)$/m) || [, ""])[1].trim();
        if (!data) continue;
        let payload = null; try { payload = JSON.parse(data); } catch { continue; }
        if (type === "session" && opts.onSession) opts.onSession(payload);
        else if (type === "token" && opts.onToken) opts.onToken(payload.text || "");
        else if (type === "done" && opts.onDone) opts.onDone(payload);
        else if (type === "error" && opts.onError) opts.onError(payload.message || "unknown error");
      }
    }
  }

  /** WebSocket chat — returns a handle with send(text) and close(). */
  chatSocket(opts = {}) {
    if (typeof WebSocket === "undefined") throw new Error("WebSocket not available in this runtime");
    const url = new URL(this.baseUrl.replace(/^http/, "ws") + "/v1/ws/chat");
    url.searchParams.set("api_key", this.apiKey);
    if (opts.conversationId) url.searchParams.set("conversation_id", opts.conversationId);
    if (opts.userId || this.userId) url.searchParams.set("user_id", opts.userId || this.userId);

    const ws = new WebSocket(url.toString());
    ws.onmessage = (ev) => {
      let m = null; try { m = JSON.parse(ev.data); } catch { return; }
      if (m.type === "session_start" && opts.onOpen) opts.onOpen(m);
      else if (m.type === "token" && opts.onToken) opts.onToken(m.text);
      else if (m.type === "done" && opts.onDone) opts.onDone(m);
      else if (m.type === "error" && opts.onError) opts.onError(m.text);
    };
    ws.onclose = (ev) => opts.onClose && opts.onClose({ code: ev.code, reason: ev.reason });

    return {
      send: (text) => ws.send(JSON.stringify({ type: "user_message", text })),
      close: () => ws.close(),
    };
  }

  // ---- documents ---------------------------------------------------------

  listDocuments() { return this._json("GET", "/v1/documents"); }

  async uploadDocument(file, filename) {
    if (typeof FormData === "undefined") throw new Error("FormData not available; upload requires browser or Node 20+");
    const fd = new FormData();
    fd.append("file", file, filename || file.name || "unnamed");
    const res = await this._fetch(this.baseUrl + "/v1/documents", {
      method: "POST",
      headers: this._headers(),
      body: fd,
    });
    const text = await res.text();
    let data = null; try { data = text ? JSON.parse(text) : null; } catch { data = { error: text }; }
    if (!res.ok) throw new PlatformError((data && (data.error || data.detail)) || res.statusText, res.status);
    return data;
  }

  deleteDocument(id) {
    return this._json("DELETE", "/v1/documents/" + encodeURIComponent(id));
  }

  // ---- analytics ---------------------------------------------------------

  analyticsSummary(days = 7) {
    return this._json("GET", "/v1/analytics/summary?days=" + encodeURIComponent(String(days)));
  }
}

export class PlatformError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "PlatformError";
    this.status = status;
  }
}

export default PlatformClient;
