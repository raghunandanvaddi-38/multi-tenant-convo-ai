/*
 * AI Platform embeddable widget
 *
 * Usage:
 *   <script src="https://your-platform.com/static/widget.js"></script>
 *   <script>
 *     Platform.init({ apiKey: "sk_...", baseUrl: "https://your-platform.com" });
 *   </script>
 *
 * The widget:
 *   - Fetches branding from GET /v1/workspace (auth: apiKey)
 *   - Renders a floating button + popup chat
 *   - Streams responses via /v1/chat/stream (SSE)
 *   - Persists conversation_id in sessionStorage
 *
 * Single file, no build step, no external CSS. Shadow DOM isolates styles
 * so the host page can't accidentally style widget internals.
 */

(function () {
  "use strict";

  if (window.Platform && window.Platform.__initialized) return;

  const Platform = (window.Platform = window.Platform || {});
  Platform.__initialized = false;

  Platform.init = async function (opts) {
    opts = opts || {};
    const apiKey = opts.apiKey;
    const baseUrl = (opts.baseUrl || "").replace(/\/+$/, "") || guessBase();
    const userId = opts.userId || "";
    const voiceEnabled = opts.voice !== false;   // default: show mic button
    if (!apiKey || !apiKey.startsWith("sk_")) {
      console.error("[Platform] init: missing/invalid apiKey");
      return;
    }
    Platform.__initialized = true;

    let workspace = null;
    try {
      const r = await fetch(baseUrl + "/v1/workspace", { headers: { "x-api-key": apiKey } });
      if (!r.ok) throw new Error(await r.text());
      workspace = await r.json();
    } catch (e) {
      console.error("[Platform] failed to load workspace:", e);
      return;
    }

    const brand = workspace.branding || {};
    const cfg = {
      apiKey,
      baseUrl,
      userId,
      voiceEnabled,
      botName: brand.bot_name || "Assistant",
      color: brand.primary_color || "#6aa5ff",
      welcome: brand.welcome_message || "Hi! How can I help?",
      position: brand.widget_position || "bottom-right",
      theme: brand.theme || "auto",
    };

    mountWidget(cfg);
  };

  function guessBase() {
    // Fall back to the script's own origin
    const scripts = document.getElementsByTagName("script");
    for (const s of scripts) {
      if ((s.src || "").indexOf("widget.js") >= 0) {
        const u = new URL(s.src);
        return u.protocol + "//" + u.host;
      }
    }
    return location.origin;
  }

  function mountWidget(cfg) {
    const host = document.createElement("div");
    host.setAttribute("data-platform-widget", "");
    host.style.all = "initial";
    document.body.appendChild(host);
    const root = host.attachShadow({ mode: "open" });

    root.innerHTML = css(cfg) + html(cfg);
    const $ = (sel) => root.querySelector(sel);

    const btn = $(".fab");
    const panel = $(".panel");
    const log = $(".log");
    const input = $(".input");
    const sendBtn = $(".send");
    const closeBtn = $(".close");
    const micBtn = $(".mic");

    let conversationId = sessionStorage.getItem("platform_conv") || null;
    let open = false;

    btn.addEventListener("click", () => toggle(!open));
    closeBtn.addEventListener("click", () => toggle(false));
    sendBtn.addEventListener("click", send);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
    if (micBtn) micBtn.addEventListener("click", toggleVoice);

    // ---- voice (streaming, raw PCM in + raw PCM out) ----
    // Server declares sample rates in `session_start.audio`. We capture at 16 kHz,
    // play back at whatever rate the server told us (24 kHz for Kokoro).
    let voiceState = "off";                          // off | connecting | live
    let voiceSock = null;
    let micStream = null;
    let audioIn = null, audioNode = null, audioSource = null;
    let audioOut = null, audioPlayhead = 0;
    let outRate = 24000;                             // updated from session_start
    let inRate  = 16000;
    let assistantBub = null;                         // live text bubble while streaming
    let partialBub = null;                           // live user bubble while speaking

    async function toggleVoice() {
      if (voiceState !== "off") { stopVoice(); return; }
      try {
        voiceState = "connecting";
        micBtn.classList.add("live");

        micStream = await navigator.mediaDevices.getUserMedia({
          audio: { channelCount: 1, sampleRate: inRate, echoCancellation: true, noiseSuppression: true },
        });
        audioIn = new AudioContext({ sampleRate: inRate });
        audioOut = new AudioContext();               // real rate set on session_start
        audioPlayhead = 0;
        audioSource = audioIn.createMediaStreamSource(micStream);
        // 512-sample buffer ≈ 32 ms — small enough that VAD reacts within a frame.
        audioNode = audioIn.createScriptProcessor(512, 1, 1);

        const url = new URL(cfg.baseUrl.replace(/^http/, "ws") + "/v1/ws/voice");
        url.searchParams.set("api_key", cfg.apiKey);
        if (cfg.userId) url.searchParams.set("user_id", cfg.userId);

        voiceSock = new WebSocket(url.toString());
        voiceSock.binaryType = "arraybuffer";

        voiceSock.onopen = () => { voiceState = "live"; };
        voiceSock.onclose = () => stopVoice();

        voiceSock.onmessage = (ev) => {
          if (typeof ev.data !== "string") {
            // Raw int16 PCM at outRate — schedule directly, no decode step
            playPCM(ev.data);
            return;
          }
          let m = null; try { m = JSON.parse(ev.data); } catch { return; }
          if (m.type === "session_start") {
            const rate = m.audio && m.audio.output && m.audio.output.sample_rate;
            if (rate && rate !== outRate) {
              outRate = rate;
              try { audioOut.close(); } catch {}
              audioOut = new AudioContext({ sampleRate: rate });
              audioPlayhead = 0;
            }
          } else if (m.type === "partial_transcript") {
            if (!partialBub) partialBub = addMsg("u", m.text);
            else partialBub.textContent = m.text;
            log.scrollTop = 1e9;
          } else if (m.type === "transcript") {
            if (partialBub) partialBub.textContent = m.text;
            else addMsg("u", m.text);
            partialBub = null;
            assistantBub = null;
          } else if (m.type === "token") {
            if (!assistantBub) assistantBub = addMsg("a", m.text);
            else { assistantBub.textContent += m.text; log.scrollTop = 1e9; }
          } else if (m.type === "done") {
            assistantBub = null;
          } else if (m.type === "error") {
            addMsg("a", "(error: " + m.text + ")");
            assistantBub = null; partialBub = null;
          }
        };

        audioNode.onaudioprocess = (e) => {
          if (!voiceSock || voiceSock.readyState !== 1) return;
          const f32 = e.inputBuffer.getChannelData(0);
          const i16 = new Int16Array(f32.length);
          for (let i = 0; i < f32.length; i++) {
            const s = Math.max(-1, Math.min(1, f32[i]));
            i16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
          }
          voiceSock.send(i16.buffer);
        };

        audioSource.connect(audioNode);
        audioNode.connect(audioIn.destination);
      } catch (e) {
        console.error("[Platform] voice failed:", e);
        addMsg("a", "Mic access denied or unavailable: " + e.message);
        stopVoice();
      }
    }

    /**
     * Play a raw int16 PCM binary chunk (little-endian) at outRate.
     * We convert to float32, wrap in an AudioBuffer, and schedule it back-to-back
     * against a running `audioPlayhead` so chunks concatenate without gaps.
     */
    function playPCM(arrayBuffer) {
      if (!arrayBuffer || arrayBuffer.byteLength < 2) return;
      const i16 = new Int16Array(arrayBuffer);
      const f32 = new Float32Array(i16.length);
      for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
      const buffer = audioOut.createBuffer(1, f32.length, outRate);
      buffer.getChannelData(0).set(f32);
      const src = audioOut.createBufferSource();
      src.buffer = buffer;
      src.connect(audioOut.destination);
      const now = audioOut.currentTime;
      const t = Math.max(now, audioPlayhead || now);
      src.start(t);
      audioPlayhead = t + buffer.duration;
    }

    function stopVoice() {
      voiceState = "off";
      if (micBtn) micBtn.classList.remove("live");
      partialBub = null; assistantBub = null;
      try { audioNode && audioNode.disconnect(); } catch {}
      try { audioSource && audioSource.disconnect(); } catch {}
      try { micStream && micStream.getTracks().forEach(t => t.stop()); } catch {}
      try { audioIn && audioIn.close(); } catch {}
      try { audioOut && audioOut.close(); } catch {}
      try { voiceSock && voiceSock.close(); } catch {}
      micStream = null; audioIn = null; audioOut = null;
      audioNode = null; audioSource = null; voiceSock = null;
    }

    if (!sessionStorage.getItem("platform_greeted")) {
      addMsg("a", cfg.welcome);
      sessionStorage.setItem("platform_greeted", "1");
    }

    function toggle(v) {
      open = v;
      panel.classList.toggle("open", open);
      if (open) input.focus();
    }

    function addMsg(role, text) {
      const div = document.createElement("div");
      div.className = "msg " + (role === "u" ? "u" : "a");
      const bub = document.createElement("div");
      bub.className = "bub";
      bub.textContent = text;
      div.appendChild(bub);
      log.appendChild(div);
      log.scrollTop = 1e9;
      return bub;
    }

    async function send() {
      const msg = input.value.trim();
      if (!msg) return;
      addMsg("u", msg);
      input.value = "";
      const bub = addMsg("a", "…");

      try {
        const r = await fetch(cfg.baseUrl + "/v1/chat/stream", {
          method: "POST",
          headers: { "content-type": "application/json", "x-api-key": cfg.apiKey },
          body: JSON.stringify({ message: msg, conversation_id: conversationId, user_id: cfg.userId }),
        });
        if (!r.ok || !r.body) {
          bub.textContent = "Error: " + r.status + " " + (await r.text());
          return;
        }
        bub.textContent = "";

        const reader = r.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });

          let idx;
          while ((idx = buf.indexOf("\n\n")) >= 0) {
            const evt = buf.slice(0, idx); buf = buf.slice(idx + 2);
            const type = (evt.match(/^event:\s*(.+)$/m) || [, ""])[1].trim();
            const data = (evt.match(/^data:\s*(.+)$/m) || [, ""])[1].trim();
            if (!data) continue;
            try {
              const d = JSON.parse(data);
              if (type === "session" && d.conversation_id) {
                conversationId = d.conversation_id;
                sessionStorage.setItem("platform_conv", conversationId);
              } else if (type === "token") {
                bub.textContent += d.text;
                log.scrollTop = 1e9;
              } else if (type === "error") {
                bub.textContent += "\n(error: " + d.message + ")";
              }
            } catch {}
          }
        }
      } catch (e) {
        bub.textContent = "Error: " + e.message;
      }
    }
  }

  // ------------------------------------------------------------
  function html(cfg) {
    return `
      <button class="fab" aria-label="Open chat" title="${escape(cfg.botName)}">
        <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
        </svg>
      </button>
      <div class="panel">
        <div class="head">
          <div class="title">${escape(cfg.botName)}</div>
          <button class="close" aria-label="Close">✕</button>
        </div>
        <div class="log"></div>
        <div class="row">
          ${cfg.voiceEnabled ? `<button class="mic" aria-label="Talk" title="Push to talk (toggles)">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 1a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
              <path d="M19 10v1a7 7 0 0 1-14 0v-1"/><line x1="12" y1="19" x2="12" y2="23"/>
              <line x1="8" y1="23" x2="16" y2="23"/>
            </svg>
          </button>` : ``}
          <textarea class="input" rows="1" placeholder="Type a message..."></textarea>
          <button class="send" aria-label="Send">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>
            </svg>
          </button>
        </div>
      </div>
    `;
  }

  function css(cfg) {
    const [vSide, hSide] = cfg.position.split("-");
    const light = cfg.theme === "light";
    const dark = cfg.theme === "dark";
    const auto = cfg.theme === "auto";
    return `<style>
      :host { all: initial; }
      * { box-sizing: border-box; font: 14px/1.4 -apple-system,system-ui,Segoe UI,Roboto,sans-serif; }
      :host {
        --p-color: ${cfg.color};
        --p-bg: #ffffff;
        --p-fg: #0f1115;
        --p-muted: #5b6478;
        --p-border: #e3e6ee;
        --p-panel2: #f4f6fb;
      }
      ${auto ? `@media (prefers-color-scheme: dark){:host{
        --p-bg:#171a21;--p-fg:#e6e8ee;--p-muted:#8b93a7;--p-border:#2a2f3a;--p-panel2:#1d212a;}}` : ""}
      ${dark ? `:host{--p-bg:#171a21;--p-fg:#e6e8ee;--p-muted:#8b93a7;--p-border:#2a2f3a;--p-panel2:#1d212a;}` : ""}

      .fab {
        position: fixed; ${vSide}: 20px; ${hSide}: 20px;
        width: 56px; height: 56px; border-radius: 50%;
        background: var(--p-color); color: #fff; border: none;
        display: flex; align-items: center; justify-content: center;
        cursor: pointer; box-shadow: 0 4px 16px rgba(0,0,0,0.2);
        z-index: 2147483000;
        transition: transform 0.15s;
      }
      .fab:hover { transform: scale(1.05); }

      .panel {
        position: fixed; ${vSide}: 88px; ${hSide}: 20px;
        width: 380px; max-width: calc(100vw - 40px);
        height: 560px; max-height: calc(100vh - 120px);
        background: var(--p-bg); color: var(--p-fg);
        border: 1px solid var(--p-border); border-radius: 12px;
        box-shadow: 0 12px 40px rgba(0,0,0,0.25);
        display: flex; flex-direction: column;
        opacity: 0; pointer-events: none; transform: translateY(10px);
        transition: opacity 0.15s, transform 0.15s;
        z-index: 2147483000; overflow: hidden;
      }
      .panel.open { opacity: 1; pointer-events: auto; transform: translateY(0); }

      .head {
        display: flex; align-items: center; justify-content: space-between;
        padding: 12px 14px;
        background: var(--p-color); color: #fff;
      }
      .head .title { font-weight: 600; font-size: 14px; }
      .head .close {
        background: transparent; color: #fff; border: none; cursor: pointer;
        font-size: 16px; opacity: 0.85;
      }
      .head .close:hover { opacity: 1; }

      .log {
        flex: 1; overflow-y: auto; padding: 12px 14px; background: var(--p-panel2);
      }
      .msg { margin-bottom: 10px; }
      .msg.u { text-align: right; }
      .bub {
        display: inline-block; padding: 8px 12px; border-radius: 14px;
        max-width: 82%; text-align: left; background: var(--p-bg);
        border: 1px solid var(--p-border); white-space: pre-wrap; word-wrap: break-word;
      }
      .msg.u .bub { background: var(--p-color); color: #fff; border-color: var(--p-color); }

      .row {
        display: flex; gap: 8px; padding: 10px 12px;
        border-top: 1px solid var(--p-border); background: var(--p-bg);
      }
      .input {
        flex: 1; padding: 8px 10px; border-radius: 8px; resize: none;
        border: 1px solid var(--p-border); background: var(--p-bg); color: var(--p-fg);
        font: inherit; max-height: 100px;
      }
      .send, .mic {
        border: none; background: var(--p-color); color: #fff;
        border-radius: 8px; padding: 0 12px; cursor: pointer;
        display: flex; align-items: center;
      }
      .mic {
        background: transparent; color: var(--p-fg);
        border: 1px solid var(--p-border);
      }
      .mic.live { background: #ef4444; color: #fff; border-color: #ef4444; }
      .mic.live svg { animation: pulse 1s infinite; }
      @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
      .send:hover, .mic:hover { filter: brightness(1.05); }
    </style>`;
  }

  function escape(s) {
    return String(s || "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  }
})();
