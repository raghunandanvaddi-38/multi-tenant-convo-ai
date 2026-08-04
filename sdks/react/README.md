# @platform/react

React hook + drop-in widget for the AI Platform.

```bash
npm install @platform/react @platform/sdk react
```

## `<AssistantWidget />` — drop-in

```jsx
import { AssistantWidget } from "@platform/react";

export default function App() {
  return (
    <>
      <YourApp />
      <AssistantWidget
        apiKey="sk_..."
        baseUrl="https://ai.example.com"
      />
    </>
  );
}
```

Renders nothing visible in your React tree — the widget mounts itself into
`document.body` inside a Shadow DOM so it can't collide with your styles.
Branding (colors, name, position, welcome message) comes from your workspace.

## `useChat` — build your own UI

```jsx
import { useMemo } from "react";
import { PlatformClient } from "@platform/sdk";
import { useChat } from "@platform/react";

export function MyChat() {
  const client = useMemo(() => new PlatformClient({
    apiKey: process.env.NEXT_PUBLIC_KEY,
    baseUrl: process.env.NEXT_PUBLIC_HOST,
  }), []);
  const { messages, isStreaming, send, error } = useChat(client);
  const [text, setText] = useState("");

  return (
    <div>
      {messages.map((m, i) => (
        <div key={i} className={m.role}>{m.text}</div>
      ))}
      {error && <div className="err">{error}</div>}
      <input
        value={text}
        onChange={e => setText(e.target.value)}
        onKeyDown={e => e.key === "Enter" && (send(text), setText(""))}
        disabled={isStreaming}
      />
    </div>
  );
}
```

## Security note

For production, don't ship raw API keys in browser JS. Mint a short-lived
signed URL from your backend and pass that to `AssistantWidget`. A future SDK
release will support a `getAuth()` async callback for this.
