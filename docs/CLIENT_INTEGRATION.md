# Client integration guide

This guide is for teams embedding the conversational AI on their own website.
Your knowledge base, prompts, and voice are configured on our side; you only
need to open a WebSocket (voice) or hit a REST endpoint (text) and render the
results.

---

## 1. Onboarding

Before you can integrate you need three things from the platform team:

| Value | Example | Where it comes from |
|---|---|---|
| **Base URL** | `https://ai.example.com` | Platform ops gives you this |
| **`tenant_id`** | `acme` | Assigned when your tenant is created |
| **`api_key`** | `k9x…tYq` (43 chars) | Rotated by admin, shown **once** — store it in your backend, never in browser JS |

### Providing your knowledge base

Send your source docs to the platform team. Supported formats: `.txt`, `.pdf`,
`.docx`. They are indexed once and used to answer questions about your
product. Re-index whenever the docs change.

### What the platform team configures per tenant

- LLM provider + model (default: Ollama running `llama3.2:3b`)
- Embedding model (default: `all-MiniLM-L6-v2`)
- System prompt (your company name, tone, off-topic policy)
- Default voice (Kokoro `af_heart` by default)
- Feature flags (backchannels, speaker verification)

Ask them if you want any of these changed.

---

## 2. Security model

- **Never call an LLM provider (Ollama / Gemini / OpenAI) directly from your
  website.** Every AI request goes through our server.
- The `api_key` authenticates a *tenant*, not a *user*. Treat it like a
  service credential: keep it on your backend, mint short-lived session
  URLs for the browser if you need to.
- If your tenant has no `api_key_hash` configured, we accept requests without
  a key — this is only appropriate for internal / development environments.
- Rotate the key via the admin UI whenever a copy might have leaked. Old key
  stops working immediately.

---

## 3. Request paths

All routes are relative to your Base URL.

### 3.1 REST

Send identity as **HTTP headers**:

```
x-tenant-id: acme
x-api-key:   <your-api-key>            # required if tenant has a key configured
x-user-id:   user-1234                 # optional, for your own audit trail
x-conversation-id: conv-abcd           # optional, keeps memory per conversation
```

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness + configured tenants |
| `GET` | `/tenants/me` | Echoes the tenant your headers resolve to — good for smoke-testing your integration |
| `POST` | `/stt` | Upload a WAV file → `{transcript, usable}` |
| `POST` | `/tts` | JSON `{text, voice?}` → `audio/wav` binary |
| `GET`  | `/session/{session_id}` | Session state (intent, emotion, turns) |
| `GET`  | `/session/{session_id}/history` | Full conversation history |
| `POST` | `/session/{session_id}/voice` | Change the TTS voice for a session |

`session_id` is the id you receive in `session_start` over the WebSocket.

### 3.2 WebSocket (voice conversations)

Open a WebSocket to:

```
wss://ai.example.com/ws?tenant_id=acme&api_key=<key>&user_id=user-1234
```

Query parameters:

| Param | Required | Notes |
|---|---|---|
| `tenant_id` | recommended | Missing = default tenant (usually `technodysis` — do not rely on this in production) |
| `api_key` | if tenant has a key | Missing/invalid → server closes with code **4401** |
| `user_id` | optional | Free-form; used only for logging + audit |

> The WebSocket streams **raw PCM audio** in binary frames and **JSON control
> messages** in text frames. See section 4 for the message protocol.

---

## 4. WebSocket protocol

### 4.1 Client → Server

**Binary frames.** Raw 16-bit little-endian PCM, **mono, 16000 Hz sample rate**.
Send in ≈32 ms chunks (roughly 1024 samples = 2048 bytes) as the user speaks.

**Text frames.** JSON control messages:

| `type` | Fields | When to send |
|---|---|---|
| `interrupt` | — | User taps a "stop speaking" button while AI is talking. (Note: the server also detects barge-in from the primary speaker automatically — you rarely need this.) |
| `set_voice` | `voice`, `provider?` (`kokoro` or `edge`) | User picks a different voice |
| `reset` | — | Clear session state, re-enroll speaker |
| `ping` | — | Optional liveness check |
| `pong` | — | Reply to server's `server_ping` (keeps the connection alive) |

### 4.2 Server → Client

All are JSON text frames **except** audio, which is binary.

| `type` | Fields | Meaning |
|---|---|---|
| `session_start` | `session_id`, `tts_engine`, `tts_voice`, `tts_provider` | Sent once on connect — store `session_id` for REST calls |
| `status` | `text` ∈ `idle` / `listening` / `thinking` / `speaking` | Pipeline state — drive your UI from this |
| `speaker` | `label`, `accepted?`, `target?`, `score?` | Enrollment progress or per-utterance verification result |
| `transcript` | `text`, `stt_ms` | STT result for the just-completed utterance |
| `ai_stream` | `text` | Streaming LLM token — append to your transcript pane |
| `ai_done` | `session`, `latency: {stt_ms, llm_ms, tts_ms, total_ms}` | End of AI turn — full metrics |
| `interrupt` | `reason`, `gen_id` | AI response was cut off (barge-in) |
| `backchannel` | `text` | Filler word ("okay…") — also followed by a small audio frame |
| `voice_changed` | `voice`, `provider` | Ack for `set_voice` |
| `reset_ack` | — | Ack for `reset` |
| `session_update` | `session` | Fresh session state after AI finishes speaking |
| `server_ping` | — | Reply with a `pong` — otherwise you'll be disconnected after ~2 misses |
| `error` | `text` | Something went wrong |
| **binary frame** | (raw bytes) | WAV/PCM audio for a TTS chunk — play it |

### 4.3 Recommended state machine

```
      ┌──────────► idle ◄────────────────────┐
      │             │                        │
      │        user starts talking           │
      │             ▼                        │
      │         listening ── silence ──► thinking
      │             ▲                        │
      │      user resumes                    ▼
      │             │                    speaking
      │      user barge-in                   │
      └── interrupt / ai_done ◄──────────────┘
```

---

## 5. Browser example — voice, minimal

```html
<!doctype html>
<meta charset="utf-8">
<button id="start">Start</button><button id="stop" disabled>Stop</button>
<pre id="log"></pre>
<script type="module">
const BASE = "wss://ai.example.com";
const TENANT = "acme";
// NOTE: in production, mint a short-lived signed URL server-side rather than
// exposing your api_key in browser JS.
const API_KEY = "k9x...tYq";

const $log = document.getElementById("log");
const log = (m) => { $log.textContent += m + "\n"; $log.scrollTop = 1e9; };

let ws, audioCtx, source, node, playhead = 0;

document.getElementById("start").onclick = async () => {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, sampleRate: 16000, echoCancellation: true }
  });

  audioCtx = new AudioContext({ sampleRate: 16000 });
  playhead = audioCtx.currentTime;
  source = audioCtx.createMediaStreamSource(stream);
  node = audioCtx.createScriptProcessor(1024, 1, 1);   // small chunk = low latency

  ws = new WebSocket(
    `${BASE}/ws?tenant_id=${TENANT}&api_key=${encodeURIComponent(API_KEY)}`
  );
  ws.binaryType = "arraybuffer";

  ws.onopen = () => log("connected");

  ws.onmessage = async (ev) => {
    if (typeof ev.data !== "string") {           // binary → audio to play
      const wav = await new Response(ev.data).arrayBuffer();
      const buf = await audioCtx.decodeAudioData(wav);
      const src = audioCtx.createBufferSource();
      src.buffer = buf; src.connect(audioCtx.destination);
      const t = Math.max(audioCtx.currentTime, playhead);
      src.start(t); playhead = t + buf.duration;
      return;
    }
    const msg = JSON.parse(ev.data);
    switch (msg.type) {
      case "session_start":  log(`session ${msg.session_id}`); break;
      case "status":         log(`[${msg.text}]`); break;
      case "transcript":     log(`you: ${msg.text}`); break;
      case "ai_stream":      $log.textContent += msg.text; break;
      case "ai_done":        log(`\n(${msg.latency.total_ms}ms)`); break;
      case "server_ping":    ws.send(JSON.stringify({ type: "pong" })); break;
      case "error":          log(`error: ${msg.text}`); break;
    }
  };

  ws.onclose = (e) => log(`closed ${e.code} ${e.reason}`);

  node.onaudioprocess = (e) => {
    if (ws.readyState !== 1) return;
    const f32 = e.inputBuffer.getChannelData(0);
    const i16 = new Int16Array(f32.length);
    for (let i = 0; i < f32.length; i++) {
      const s = Math.max(-1, Math.min(1, f32[i]));
      i16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    ws.send(i16.buffer);
  };

  source.connect(node);
  node.connect(audioCtx.destination);
  document.getElementById("stop").disabled = false;
};

document.getElementById("stop").onclick = () => {
  node?.disconnect(); source?.disconnect(); audioCtx?.close();
  ws?.close();
  document.getElementById("stop").disabled = true;
};
</script>
```

Notes:

- **Sample rate must be 16000 Hz.** If the browser can't give you 16 kHz
  directly, resample in a `ScriptProcessorNode` / `AudioWorkletNode` before
  sending. Servers reject other rates silently — you'll just hear nothing.
- **Barge-in works automatically** — the server verifies the primary speaker
  and cuts the AI off if the same user speaks over it.
- **Audio playback**: the server sends TTS as WAV binary frames. Decode with
  `decodeAudioData` and schedule sequentially to avoid gaps.
- **Reconnects**: if the connection drops, open a new WebSocket. Session
  state does not survive a reconnect; use `reset` to clear it explicitly.

---

## 6. REST examples

### Health + tenant check

```bash
curl https://ai.example.com/health
curl -H "x-tenant-id: acme" -H "x-api-key: $KEY" \
     https://ai.example.com/tenants/me
```

### Transcribe an audio file

```bash
curl -X POST https://ai.example.com/stt \
  -H "x-tenant-id: acme" -H "x-api-key: $KEY" \
  -F "audio=@sample.wav"
# → {"transcript": "hello there", "usable": true}
```

### Synthesize speech

```bash
curl -X POST https://ai.example.com/tts \
  -H "x-tenant-id: acme" -H "x-api-key: $KEY" \
  -H "content-type: application/json" \
  -d '{"text": "Hello from Acme", "voice": "af_heart"}' \
  -o out.wav
```

### Fetch conversation history

```bash
curl -H "x-tenant-id: acme" -H "x-api-key: $KEY" \
     https://ai.example.com/session/$SESSION_ID/history
```

---

## 7. Configuration knobs (per tenant)

You can request changes to any of these — none of them touch your integration
code, only the tenant config on the server.

| Group | Setting | Notes |
|---|---|---|
| Identity | `display_name` | Shown in admin UI |
| LLM | `provider`, `model`, `base_url`, `temperature`, `max_tokens`, `context_window` | Provider is currently `ollama`; more can be added by the platform |
| Prompt | `prompt_template` | Must contain `{conversation_history}`, `{context}`, `{query}` placeholders |
| RAG | `embedding_model`, `knowledge_dir`, `chunk_size`, `top_k`, `fuzzy_match_threshold`, `entity_corrections` | `entity_corrections` maps misspellings of your brand name to the canonical form |
| TTS | `provider` (`kokoro` / `edge`), `default_voice` | Kokoro voices: `af_heart`, `af_bella`, `am_adam`, `am_michael`; Edge voices: MS neural names |
| STT | `model`, `language` | Default: Qwen ASR, English |
| Features | `backchannels`, `speaker_verification` | Boolean toggles |

---

## 8. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| WebSocket closes with code **4401** immediately | Missing or wrong `api_key` | Check the query string; rotate the key if you're not sure it's correct |
| REST returns **401** | Same as above, header form | Send `x-api-key` header |
| REST returns **404** on `/tenants/me` | `tenant_id` typo | Verify with `GET /health` — response includes the tenant list |
| WebSocket connects but no audio flows | Wrong sample rate | Confirm the browser is capturing at 16 kHz mono int16; resample if not |
| `status: thinking` never advances to `speaking` | LLM is down or knowledge base failed to load | Check with the platform team; `GET /health` shows the LLM engine |
| AI answers off-topic questions | Prompt template too permissive | Ask platform team to tighten `prompt_template` — e.g. add "For off-topic questions, redirect to …" |
| AI keeps saying "I don't have that info" for things in your docs | Docs weren't indexed, or indexed under a different tenant | Ask platform team to reindex; verify `rag.knowledge_dir` in the admin UI |
| Voice sounds wrong | Session voice differs from tenant default | `POST /session/{id}/voice` or send `set_voice` over the WebSocket |
| Disconnected after ~50 s of silence | You aren't replying to `server_ping` | Send `{"type":"pong"}` when you receive `{"type":"server_ping"}` |

---

## 9. What NOT to do

- Do not call Ollama, Gemini, OpenAI, or any LLM provider directly from your
  frontend. Every AI request must go through our server so we can enforce
  tenant isolation and usage limits.
- Do not persist `api_key` in browser storage. Fetch a signed URL from your
  backend at connection time instead.
- Do not fabricate `session_id`s or reuse them across users — the server
  mints one per WebSocket connection.
- Do not send audio at a rate other than 16 kHz mono. Resample first.

---

## 10. Support checklist

When opening a support ticket, please include:

1. Your `tenant_id`
2. The `session_id` from the failing conversation
3. Approximate UTC timestamp
4. What you did → what you expected → what happened
5. Any `error` message text from the server
