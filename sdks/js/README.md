# @platform/sdk

Client SDK for the AI Platform. Works in browser and Node 18+.

```bash
npm install @platform/sdk
```

```js
import { PlatformClient } from "@platform/sdk";

const client = new PlatformClient({
  apiKey: "sk_...",                    // create one in the dashboard
  baseUrl: "https://ai.example.com",   // your platform host
});

// Single-shot chat
const { reply, conversation_id } = await client.chat("Hello!");
console.log(reply);

// Streaming chat (SSE)
await client.chatStream("Tell me a joke", {
  conversationId: conversation_id,
  onToken: (t) => process.stdout.write(t),
  onDone: (d) => console.log(`\n(${d.latency_ms}ms)`),
});

// WebSocket chat
const sock = client.chatSocket({
  onOpen: (info) => console.log("session:", info.conversation_id),
  onToken: (t) => process.stdout.write(t),
  onDone: () => console.log("\ndone"),
});
sock.send("What are your business hours?");
```

## Documents

```js
const docs = await client.listDocuments();

// Browser file upload
await client.uploadDocument(fileInputEl.files[0]);

// Node: fs.readFileSync + Blob (Node 20+)
```

## Analytics

```js
const summary = await client.analyticsSummary(7);
// { total_messages, avg_latency_ms, top_queries, ... }
```

## Scopes

Your API key needs the right scope for each call:

| Call | Required scope |
|---|---|
| `chat`, `chatStream`, `chatSocket` | `chat` |
| `listDocuments`, `analyticsSummary` | `read` |
| `uploadDocument`, `deleteDocument` | `admin` |

An `admin` key implies all others.

## Errors

All API errors throw `PlatformError` with `.status` set to the HTTP code.

## React

For a drop-in `<AssistantWidget />` component, see [@platform/react](../react/README.md).
