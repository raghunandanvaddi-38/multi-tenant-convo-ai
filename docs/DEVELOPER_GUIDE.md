# Developer guide

For engineers building a conversational AI product on top of the platform.

This guide assumes you have a running platform instance (yours or ours) and
covers everything from the first API call to shipping a production widget.
For end-user integration in ~200 lines see [CLIENT_INTEGRATION.md](CLIENT_INTEGRATION.md).

---

## 1. Mental model

```
Organization                one billing/security boundary
  └── Workspace             one assistant — its own docs, prompt, model, keys
        ├── Documents       PDFs, DOCX, TXT, MD, CSV, XLSX, HTML
        ├── Prompt          template with {conversation_history}{context}{query}
        ├── Model config    LLM provider/model/temperature/etc
        ├── Branding        widget colors, name, welcome, position, theme
        ├── API keys        sk_… — scoped: chat / read / admin
        └── Conversations   memory keyed by (workspace_id, conversation_id)
```

**Rules that never bend:**

- One API key belongs to exactly one workspace. Workspaces cannot see each other's data.
- Clients never talk to the LLM directly. Every AI request goes through the platform.
- API keys are hashed at rest; the plaintext is shown once at creation.
- The platform is stateless per-request except for conversation memory (keyed by `conversation_id`, which *you* pass on each call).

---

## 2. Get from zero to a working chat in 5 minutes

### 2.1 Sign up + create an assistant

Point a browser at `https://<host>/dashboard`, sign up, and you're dropped into a workspace called `default`.

Then in the dashboard:

1. **Documents** → upload your knowledge base (needs an `admin`-scope key; the "Documents" tab tells you which scope you need)
2. **Prompt** → adjust the template if you want; the default is a safe conversational assistant
3. **Branding** → set the bot name, colour, welcome message
4. **API keys** → create one with `chat,read` scope → copy the `sk_…` value (**shown once**)

### 2.2 Call it

```bash
curl -X POST https://<host>/v1/chat \
  -H "x-api-key: sk_..." \
  -H "content-type: application/json" \
  -d '{"message": "What are your business hours?"}'
```

Response:

```json
{
  "reply": "Our support team is available 24/7 at support@example.com.",
  "conversation_id": "a1b2c3d4",
  "workspace_id": "wksp_...",
  "latency_ms": 1240
}
```

Pass `conversation_id` back on the next call to keep memory:

```bash
curl -X POST https://<host>/v1/chat \
  -H "x-api-key: sk_..." -H "content-type: application/json" \
  -d '{"message": "How do I reach you by phone?", "conversation_id": "a1b2c3d4"}'
```

---

## 3. The transport options

Pick whichever fits your product:

| Transport | Path | Use when |
|---|---|---|
| **REST single-shot** | `POST /v1/chat` | Batch jobs, integrations, low-frequency chat, testing |
| **SSE stream** | `POST /v1/chat/stream` | Browser or Node app that renders tokens as they arrive — the "typing" effect |
| **WebSocket (text)** | `WS /v1/ws/chat` | Full-duplex text apps, mobile, apps that also need push from server |
| **WebSocket (voice)** | `WS /v1/ws/voice` | Real-time voice conversations — streaming STT, streaming TTS, raw PCM |
| **REST voice one-shot** | `POST /v1/stt`, `POST /v1/tts` | Batch transcription, voice generation for TTS-only or STT-only flows |

### 3.1 REST — the simplest thing that could possibly work

Request:

```http
POST /v1/chat
x-api-key: sk_...
content-type: application/json

{ "message": "Hello", "conversation_id": "c1", "user_id": "u_42" }
```

Response is a single JSON blob (see 2.2).

### 3.2 SSE — for streaming UIs

Same request shape, hit `/v1/chat/stream`. Response is `text/event-stream`:

```
event: session
data: {"conversation_id": "a1b2c3d4"}

event: token
data: {"text": "Our "}

event: token
data: {"text": "support "}

...

event: done
data: {"latency_ms": 1240}
```

Any HTTP client that supports streaming will work — `fetch().body.getReader()` in the browser, `httpx.AsyncClient.stream()` in Python, etc. Node.js 18+ works too:

```js
const res = await fetch("https://<host>/v1/chat/stream", {
  method: "POST",
  headers: { "x-api-key": KEY, "content-type": "application/json" },
  body: JSON.stringify({ message: "Hello", conversation_id: convId }),
});
for await (const chunk of res.body) {
  // parse SSE frames — see @platform/sdk for a battle-tested parser
}
```

### 3.3 WebSocket — for full-duplex apps

```
ws://<host>/v1/ws/chat?api_key=sk_...&conversation_id=c1&user_id=u_42
```

Messages you receive:

| `type` | Meaning |
|---|---|
| `session_start` | Sent once. Includes `workspace_id`, `conversation_id`, `branding` — use this to theme your UI |
| `token` | One LLM token — append to the assistant bubble |
| `done` | Turn finished. Includes `latency_ms` |
| `error` | Something went wrong. `text` is user-facing |
| `pong` | Reply to your `ping` |

Messages you send:

```json
{ "type": "user_message", "text": "Hello" }
```

Ping/pong is optional but recommended for long-lived connections:

```json
{ "type": "ping" }
```

Close codes:

- `4401` — invalid or missing API key
- `4403` — API key lacks `chat` scope
- normal close if the client goes away

### 3.4 Voice — `WS /v1/ws/voice`

The voice endpoint is designed for **sub-second perceived latency** — you hear the assistant start speaking within about a second of finishing your question. It achieves this with:

- **Rolling STT.** Every ~0.8 s of speech the server transcribes the buffer so far and emits a `partial_transcript`. Final commit happens on end-of-speech.
- **Short-phrase TTS flush.** The AI's reply starts synthesising after ~22 characters or the first pause — not after a full sentence.
- **Raw int16 PCM binary frames.** No WAV header per chunk, no `decodeAudioData` on the client. Playback is direct.

Connection URL:

```
ws://<host>/v1/ws/voice?api_key=sk_...&conversation_id=c1&user_id=u_42
```

**Session preamble** — the first server message declares the audio format so you size buffers correctly:

```json
{
  "type": "session_start",
  "conversation_id": "c1",
  "workspace_id": "wksp_...",
  "branding": { "bot_name": "...", "primary_color": "..." },
  "audio": {
    "input":  { "sample_rate": 16000, "channels": 1, "encoding": "pcm_s16le" },
    "output": { "sample_rate": 24000, "channels": 1, "encoding": "pcm_s16le" }
  },
  "streaming": { "partial_interval_s": 0.8, "end_silence_s": 1.0 }
}
```

**Client → server**

| Type | Body | Meaning |
|---|---|---|
| _binary_ | raw int16 PCM, mono, at `input.sample_rate` | Mic audio, ~32 ms chunks |
| `user_text` | `{"text": "..."}` | Force a turn from typed text (skips STT) |
| `flush` | — | Force end-of-utterance without waiting for silence |
| `reset` | — | Clear audio buffer + conversation memory |
| `ping` | — | Keepalive |

**Server → client**

| Type | Fields | Meaning |
|---|---|---|
| `session_start` | (see above) | Sent once on connect |
| `status` | `text: listening / thinking / speaking / idle` | Pipeline state |
| `partial_transcript` | `text` | Live transcript — may be revised in later `partial_transcript` events |
| `transcript` | `text` | Final committed transcription for the utterance |
| `token` | `text` | LLM streaming text (append to UI in real time) |
| _binary_ | raw int16 PCM at `output.sample_rate` | Play back-to-back to hear the assistant |
| `done` | `latency_ms` | Turn complete |
| `error` | `text` | Something went wrong |
| `pong` | — | Reply to your `ping` |

**Minimum viable client** (browser, ~60 lines):

```js
const audioIn  = new AudioContext({ sampleRate: 16000 });
const audioOut = new AudioContext();   // real rate arrives via session_start
let playhead = 0, outRate = 24000;

const stream = await navigator.mediaDevices.getUserMedia({
  audio: { channelCount: 1, sampleRate: 16000, echoCancellation: true },
});
const src = audioIn.createMediaStreamSource(stream);
const node = audioIn.createScriptProcessor(512, 1, 1);   // 32 ms

const ws = new WebSocket(`wss://<host>/v1/ws/voice?api_key=${API_KEY}`);
ws.binaryType = "arraybuffer";

ws.onmessage = (ev) => {
  if (typeof ev.data !== "string") return playPCM(ev.data);
  const m = JSON.parse(ev.data);
  if (m.type === "session_start")       outRate = m.audio.output.sample_rate;
  else if (m.type === "partial_transcript") liveTranscript(m.text);
  else if (m.type === "transcript")     commitTranscript(m.text);
  else if (m.type === "token")          appendAssistant(m.text);
};

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
src.connect(node); node.connect(audioIn.destination);

function playPCM(arrayBuffer) {
  const i16 = new Int16Array(arrayBuffer);
  const f32 = new Float32Array(i16.length);
  for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
  const buf = audioOut.createBuffer(1, f32.length, outRate);
  buf.getChannelData(0).set(f32);
  const s = audioOut.createBufferSource();
  s.buffer = buf; s.connect(audioOut.destination);
  const t = Math.max(audioOut.currentTime, playhead || audioOut.currentTime);
  s.start(t); playhead = t + buf.duration;
}
```

The SDK (§4.2) wraps all of this behind one call.

### 3.5 Voice — one-shot REST

For batch or non-realtime work:

```bash
# Transcribe a WAV file (any rate; auto-resampled to 16 kHz)
curl -X POST https://<host>/v1/stt \
  -H "x-api-key: sk_..." \
  -F "audio=@recording.wav"
# → {"transcript": "hello world", "usable": true, "workspace_id": "wksp_..."}

# Synthesize speech (uses the workspace's default voice if you omit "voice")
curl -X POST https://<host>/v1/tts \
  -H "x-api-key: sk_..." \
  -H "content-type: application/json" \
  -d '{"text": "Hello world"}' \
  -o out.wav
```

Voice + provider per workspace live in `settings.tts` — configurable from the dashboard's Model tab.

---

## 4. Using the JS/TS SDK

For anything more than a curl demo, use [`@platform/sdk`](../sdks/js/README.md). It handles the SSE parser, WebSocket lifecycle, retries and auth headers for you.

```bash
npm install @platform/sdk
```

```ts
import { PlatformClient } from "@platform/sdk";

const client = new PlatformClient({
  apiKey: process.env.PLATFORM_KEY!,      // NEVER hardcode in a client bundle
  baseUrl: "https://<host>",
});

// Single-shot
const { reply, conversation_id } = await client.chat("Hello");

// Streaming (browser + Node)
await client.chatStream("Tell me a joke", {
  conversationId: conversation_id,
  onToken: (t) => process.stdout.write(t),
  onDone:  ({ latency_ms }) => console.log(`\n(${latency_ms}ms)`),
  onError: (m) => console.error(m),
});

// WebSocket handle
const sock = client.chatSocket({
  onOpen:  (info) => console.log("started:", info.conversation_id),
  onToken: (t) => appendToken(t),
  onDone:  () => setStreaming(false),
});
sock.send("What are your hours?");
```

Every method returns typed data — see the shipped `.d.ts` for the full surface.

### 4.1a Voice via the SDK

Three methods cover everything:

```ts
// One-shot: file in → text
const { transcript, usable } = await client.transcribe(fileFromInput);

// One-shot: text in → audio (returns ArrayBuffer of WAV bytes)
const wavBuf = await client.synthesize("Hello from Acme");

// Streaming: full duplex, raw PCM both directions
const voice = client.voiceSocket({
  onOpen:      (info) => console.log("audio format:", info.audio),
  onStatus:    (s) => setStatus(s),                    // "listening" | "thinking" | "speaking" | "idle"
  onPartialTranscript: (t) => setLiveUserText(t),      // updates while user speaks
  onTranscript:        (t) => commitUserText(t),       // final version
  onToken:     (t) => appendAssistantText(t),
  onAudio:     (pcm) => playPCM(pcm),                  // ArrayBuffer of int16 samples
  onDone:      (info) => console.log(`${info.latency_ms}ms`),
});

// Feed mic audio (int16 PCM at 16 kHz — see §3.4 for how to capture it)
voice.sendAudio(pcm16BufferOrInt16Array);

// Or bypass STT and send typed text as a turn
voice.sendText("what are your hours?");

// Force end-of-utterance (e.g. push-to-talk release)
voice.flush();

// End the session
voice.close();
```

Every callback is optional. The SDK doesn't touch `AudioContext` — you own playback and capture so the library stays framework-agnostic. The `AssistantWidget` (§4.2) is where the fully-wired UI lives.

### 4.1 React

```bash
npm install @platform/react @platform/sdk
```

Drop-in widget (uses your workspace branding):

```tsx
import { AssistantWidget } from "@platform/react";

export default function App() {
  return (
    <>
      <YourApp />
      <AssistantWidget apiKey={process.env.NEXT_PUBLIC_KEY!} baseUrl={HOST} />
    </>
  );
}
```

Build your own UI with the `useChat` hook:

```tsx
import { useMemo, useState } from "react";
import { PlatformClient } from "@platform/sdk";
import { useChat } from "@platform/react";

function Chat({ apiKey }: { apiKey: string }) {
  const client = useMemo(() => new PlatformClient({ apiKey, baseUrl: HOST }), [apiKey]);
  const { messages, isStreaming, send, error } = useChat(client);
  const [input, setInput] = useState("");
  return (
    <>
      {messages.map((m, i) => <div key={i} className={m.role}>{m.text}</div>)}
      {error && <div className="err">{error}</div>}
      <input
        value={input} onChange={e => setInput(e.target.value)}
        onKeyDown={e => e.key === "Enter" && (send(input), setInput(""))}
        disabled={isStreaming}
      />
    </>
  );
}
```

---

## 5. Embeddable widget — the "one line of JS" path

For customers who just want the bot on their site with zero code:

```html
<script src="https://<host>/static/widget.js"></script>
<script>
  Platform.init({
    apiKey: "sk_...",
    baseUrl: "https://<host>",
    // voice: false,   // uncomment to hide the mic button (text-only)
  });
</script>
```

The widget renders in a Shadow DOM so host page styles can't touch it. All visual settings (colour, name, welcome message, position, theme) come from the **Branding** tab of the workspace. Change the branding in the dashboard → new visits pick it up immediately, no redeploy.

### 5.1 Voice out of the box

The floating chat shows a **mic button** by default. Tap it to open a streaming voice session (`WS /v1/ws/voice`) — the button pulses red while the mic is live. Everything from §3.4 runs behind the scenes: partial transcripts appear as a live user bubble, tokens stream into the assistant bubble, PCM audio plays back-to-back with no gaps.

To disable the mic (e.g. for a text-only support page):

```js
Platform.init({ apiKey: "sk_...", baseUrl: "...", voice: false });
```

### 5.2 Security note

**Do not ship your API key inline in production.** Have your backend mint short-lived widget tokens (this is a planned feature; for now, treat the key like a service credential and rotate on suspected leak).

---

## 6. Working with documents

### 6.1 Supported formats

`.pdf`, `.docx`, `.txt`, `.md`/`.markdown`, `.csv`, `.xlsx`, `.html`/`.htm`

Files up to 50 MB by default (set `MAX_UPLOAD_BYTES` env var to change). Apple Pages/Numbers/Keynote are **not** supported — export to PDF first.

### 6.2 The ingestion pipeline

```
Upload → validated → stored → Document row (status=queued)
                       │
              background task
                       ▼
     extract text (per format) →
     chunk (default: 150 words, split on '---' section markers) →
     embed (via workspace embedding model) →
     append to workspace FAISS index (persisted to disk) →
     Document row updated (status=ready, chunk_count=N)
```

If anything fails, the document row is marked `failed` with a human-readable `error` field.

### 6.3 API

```bash
# Upload (needs admin-scope key)
curl -X POST https://<host>/v1/documents \
  -H "x-api-key: sk_..." \
  -F "file=@handbook.pdf"

# List (chat/read/admin all fine)
curl https://<host>/v1/documents -H "x-api-key: sk_..."

# One document
curl https://<host>/v1/documents/{id} -H "x-api-key: sk_..."

# Reprocess a failed doc
curl -X POST https://<host>/v1/documents/{id}/reindex -H "x-api-key: sk_..."

# Delete
curl -X DELETE https://<host>/v1/documents/{id} -H "x-api-key: sk_..."
```

### 6.4 Getting good retrieval quality

- **Structure your source docs.** Put a `\n---\n` between logical sections; the chunker respects it and keeps sections together whenever possible.
- **Prefer many small docs over one giant doc.** The retriever picks `top_k` chunks (default: 2); if all chunks are lost in a 500-page tome, quality drops.
- **Give related terms.** If your product is called *Acme Pro X*, mention it that way once, and again as *Pro X*, *Acme's flagship*, so the query embedding matches whatever the user types.
- **Tune the workspace `rag.top_k`** in the dashboard's Model tab — higher = more context but slower and more tokens.
- **`entity_corrections`** maps common misspellings of your brand to the canonical form before embedding. Ask us to set it up if users type your product name three different ways.

---

## 7. Prompt engineering

The prompt template is a plain Python format string with three placeholders:

- `{conversation_history}` — last N turns, `User:` / `Assistant:` on their own lines
- `{context}` — retrieved chunks, joined with blank lines
- `{query}` — the current user question

The default template is deliberately conservative:

```
You are a professional AI assistant.

Conversation History:
{conversation_history}

Answer using only the given context in 3-4 lines. Plain flowing sentences only.

Context: {context}

Question: {query}

Answer:
```

### 7.1 Patterns that work

- **State the identity first.** "You are Acme's customer support assistant."
- **Constrain output shape.** "Reply in 2–3 sentences. Plain text — no lists, no markdown."
- **Route off-topic to a URL or handoff.** "If the question isn't about Acme, reply: 'I can only help with Acme. See www.acme.com or email hello@acme.com.'"
- **Never invent details.** "If the answer isn't in the context, say you don't know. Do not invent product names, prices, or contact details."

### 7.2 Versioning

Every save on the Prompt tab snapshots the previous version. Click **Revert** on the history entry to roll back. Versions live in the `prompt_versions` table forever — cheap to keep.

### 7.3 Testing changes

Use the **Try it** tab — it's a live chat driven by a `chat`-scope key you paste in. Every edit takes effect on the next message; no restart.

---

## 8. Multi-workspace design patterns

You have several ways to model your customers on the platform:

| Pattern | When | Cost |
|---|---|---|
| One workspace per **customer** | You sell an AI for each of your customers, each with their own docs and prompt | Simplest mental model. One key per customer, easy to rotate/revoke per customer. |
| One workspace per **product / assistant** | Same customer runs multiple assistants (sales, support, HR) | Isolation by role. Different prompts, different docs. Keys can be scoped per assistant. |
| One workspace, multiple `conversation_id`s | Everyone shares one knowledge base; conversations are per-user | Cheapest. Bad if data or prompts differ per user. |

The `conversation_id` is a free-form string — use anything up to 120 chars. Common choices:

- `user_<userId>` — one long conversation per user
- `session_<sessionId>` — new conversation per browser session
- `ticket_<ticketId>` — one conversation per support ticket

If you leave it out, the server mints a fresh id on every call — good for stateless one-shot calls, bad if you want memory.

---

## 9. Auth & security

### 9.1 API key scopes

| Scope | Grants |
|---|---|
| `chat` | `POST /v1/chat`, `POST /v1/chat/stream`, `WS /v1/ws/chat` |
| `read` | `GET /v1/workspace`, `GET /v1/documents*`, `GET /v1/analytics/summary` |
| `admin` | All of the above plus `POST/DELETE /v1/documents*`, workspace settings |

An `admin` key implies both other scopes.

### 9.2 Two auth flavors

Users authenticate one way; server-to-server another:

| Principal | How | Where |
|---|---|---|
| Human (dashboard) | `Authorization: Bearer <jwt>` from `/auth/login` | Management routes (`/v1/organizations`, `/v1/workspaces`, `/v1/analytics/workspaces/*`) |
| Workspace-bound app | `x-api-key: sk_…` header OR `Authorization: Bearer sk_…` on `/ws/chat` query string | Public API (`/v1/chat*`, `/v1/documents*`, `/v1/workspace`) |

**Never** put a JWT in a browser widget or an API key in a public git repo.

### 9.3 Key rotation

Two-step rotation with zero downtime:

1. **Dashboard → API keys → Create** — get `sk_new_...`
2. Deploy `sk_new_...` to your app
3. Verify traffic is flowing on the new key
4. **Dashboard → API keys → Revoke old**

Traffic on a revoked key immediately gets `401`.

### 9.4 Rate limits

Per-API-key defaults:

- `RATE_LIMIT_BURST` = 10 requests per second
- `RATE_LIMIT_PER_MIN` = 60 requests per minute

429 responses include a `retry-after` header. Back off, retry.

---

## 10. Error handling

Every non-2xx response has this shape:

```json
{ "error": "human readable reason" }
```

or for validation errors (422):

```json
{ "detail": [{ "loc": ["body", "password"], "msg": "String should have at least 8 characters", "type": "..." }] }
```

Recommended client behaviour:

| Status | What to do |
|---|---|
| **401** | Refresh JWT or rotate API key. Do not retry with the same credential. |
| **403** | Key is missing a required scope. Create a new key with the right scope. |
| **404** | The workspace/document/tenant id is wrong. |
| **413** | File too large. Split it, or bump `MAX_UPLOAD_BYTES` on the platform. |
| **422** | Request body validation failed. `detail` tells you which field. |
| **429** | Rate limited. Read `retry-after`, back off. |
| **5xx** | Retry with exponential backoff, cap at ~3 tries. Log the `x-request-id` from the response. |

Every response carries an `x-request-id` header — quote it in support tickets.

---

## 11. Analytics

Every message writes an `Event` row (workspace_id, model, conversation_id, tokens_out, latency_ms, query_text truncated to 500 chars).

```bash
curl https://<host>/v1/analytics/summary?days=7 -H "x-api-key: sk_..."
```

```json
{
  "days": 7,
  "total_messages": 1284,
  "unique_conversations": 342,
  "total_tokens_out": 91340,
  "avg_latency_ms": 620,
  "errors": 4,
  "top_queries": [
    { "query": "reset password", "count": 47 },
    { "query": "business hours", "count": 31 }
  ]
}
```

Use this to figure out:

- **Which questions come up most** → make sure those chunks retrieve well
- **Where latency is climbing** → check LLM warmup, embedding model, top_k
- **Whether errors are spiking** → check server logs; every error has a `request_id` you can grep

---

## 12. Production checklist

Before pointing real customers at your build:

- [ ] Set `SECRET_KEY` to a strong random ≥ 32 bytes. Rotate quarterly.
- [ ] Set `DATABASE_URL` to Postgres. SQLite is dev-only.
- [ ] Set `REDIS_URL` if you have more than one worker (rate limits and cache need shared state).
- [ ] Set `STORAGE_ROOT` to a mounted volume that survives redeploys — the FAISS indexes live there.
- [ ] Set `MAX_UPLOAD_BYTES` to what your product actually needs (default 50 MB).
- [ ] TLS terminate in front of uvicorn (nginx, Caddy, ALB — whatever).
- [ ] Enable structured JSON logging (`request_id`, `workspace_id`, `latency_ms`) — ship to your log aggregator.
- [ ] Alert on: 5xx rate > 1% for 5 min, avg chat latency > 5s for 5 min, disk usage on `STORAGE_ROOT` > 80%.
- [ ] Snapshot the DB nightly. Snapshot `STORAGE_ROOT` nightly too — losing it means re-embedding every document.
- [ ] Set `ACCESS_TOKEN_MINUTES=15` and `REFRESH_TOKEN_DAYS=7` for the dashboard if it's public-facing.
- [ ] For each workspace: create a **prod key** and a **staging key** with different scopes. Never share.
- [ ] Rotate any API key that was ever in a git commit, chat log, or terminal history.

---

## 13. Extending the platform

### 13.1 Adding a new LLM provider (e.g. Gemini)

1. Create `app/providers/gemini_provider.py` implementing the `LLMProvider` protocol:

```python
class GeminiProvider:
    name = "gemini"
    def __init__(self, api_key: str, base_url: str, api_timeout: int = 10): ...
    async def generate_stream(self, prompt: str, params) -> AsyncIterator[str]: ...
    async def warmup(self, params) -> None: ...
```

2. Register a factory in `app/gateway/router.py`:

```python
def _build_gemini(cfg): return GeminiProvider(api_key=..., base_url=cfg.base_url)
self._factories["gemini"] = _build_gemini
```

3. Workspaces set `settings.llm.provider = "gemini"` in the dashboard. That's it — no other code changes.

### 13.2 Adding a new document format

Add an extractor to `app/documents/extractors.py`:

```python
def extract_epub(data: bytes) -> str: ...
EXTRACTORS[".epub"] = extract_epub
```

The ingestion pipeline and dashboard `accept=` filter both read from `EXTRACTORS` — no other changes.

### 13.3 Adding a new storage backend (S3, GCS)

Implement the protocol in `app/storage/base.py`:

```python
class S3Backend:
    async def write(self, path, data) -> str: ...
    async def read(self, path) -> bytes: ...
    async def delete(self, path) -> None: ...
    async def exists(self, path) -> bool: ...
```

Register in `app/storage/factory.py` under a new `STORAGE_BACKEND=s3` branch. Existing routes and services are storage-agnostic.

---

## 14. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Chat returns "Sorry, something went wrong." | LLM provider is down or `base_url` wrong | Check `settings.llm.base_url`; hit `curl <base_url>/api/tags` for Ollama |
| Docs stuck in `processing` forever | Background task crashed silently; server was restarted mid-index | `POST /v1/documents/{id}/reindex` |
| "Answer isn't in my docs" for a fresh upload | Index rebuild lag; workspace RAG cache warmed against old state | Restart the process (invalidates in-memory RAG cache) |
| SSE stream reconnects every 30–60s | Reverse proxy timing out idle connections | Increase proxy `read_timeout` for `/v1/chat/stream`; set your CDN to bypass buffering for `text/event-stream` |
| Widget shows default colors | Branding hasn't been saved OR `/v1/workspace` returned an error | Open browser devtools → Network → look at `GET /v1/workspace` |
| 401 on every call the moment I start the server | `SECRET_KEY` changed since JWTs were issued; all old tokens are invalid | Sign out + sign back in |
| Rate limits triggering constantly in dev | Default 10/sec is tight | `export RATE_LIMIT_BURST=100 RATE_LIMIT_PER_MIN=6000` |

---

## 15. What's on the roadmap (not shipped yet)

Fair to name what isn't here:

- **Signed short-lived widget tokens.** Today the widget takes a raw API key. A `POST /v1/widget-token` that mints a 10-minute JWT is the right long-term shape.
- **Function calling.** The LLM can only generate text. No structured tool use yet.
- **Real WebRTC transport.** Voice uses WebSocket + raw-PCM binary frames — matches WebRTC latency on same-region networks but not on lossy mobile links. Adding an `aiortc`-based `POST /v1/rtc/offer` peer-connection endpoint is planned; the SDK surface (`voiceSocket`) is intentionally shaped so it can fall back to WebRTC transparently.
- **AudioWorklet capture.** The widget mic path currently uses `ScriptProcessorNode` (deprecated but universally supported). Migrating to `AudioWorkletNode` will reduce input jitter further.
- **Mobile SDKs.** iOS/Android/Flutter/React Native are REST-driven — treat the wire contract in this doc as the mobile SDK. Native wrappers are on the roadmap.
- **Multi-region.** Storage backend is pluggable, but the DB and Redis are single-region for now.
- **Fine-grained RBAC.** Membership roles are `owner / admin / member` at the org level. Per-workspace roles are not yet a thing.

Reach out or file an issue when any of these blocks you.
