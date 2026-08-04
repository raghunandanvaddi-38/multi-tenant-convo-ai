/**
 * @platform/react — React bindings for the AI Platform SDK.
 *
 * Exports:
 *   useChat(client, opts)  — reactive chat state hook with streaming
 *   <AssistantWidget />    — drop-in floating chat, backed by the vanilla widget
 *
 * The vanilla widget (static/widget.js on the platform) is intentionally the
 * source of truth for the visual widget — this React wrapper simply loads it
 * and calls Platform.init(). Use useChat() if you want to build your own UI.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { PlatformClient } from "@platform/sdk";

export function useChat(client, opts = {}) {
  const [messages, setMessages] = useState([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState(null);
  const [conversationId, setConversationId] = useState(opts.conversationId || null);

  const send = useCallback(async (text) => {
    if (!text || !text.trim() || isStreaming) return;
    setError(null);
    setMessages((m) => [...m, { role: "user", text }]);
    setIsStreaming(true);

    let assistantText = "";
    setMessages((m) => [...m, { role: "assistant", text: "" }]);

    const applyToken = (t) => {
      assistantText += t;
      setMessages((m) => {
        const copy = m.slice();
        copy[copy.length - 1] = { role: "assistant", text: assistantText };
        return copy;
      });
    };

    try {
      await client.chatStream(text, {
        conversationId,
        userId: opts.userId,
        onSession: (s) => setConversationId(s.conversation_id),
        onToken: applyToken,
        onError: (msg) => setError(msg),
      });
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setIsStreaming(false);
    }
  }, [client, conversationId, isStreaming, opts.userId]);

  const reset = useCallback(() => {
    setMessages([]);
    setError(null);
    setConversationId(null);
  }, []);

  return { messages, isStreaming, error, conversationId, send, reset };
}

/**
 * Drop-in widget backed by the platform's own `widget.js` (loaded once from
 * the platform origin). Uses a plain <script> tag + Platform.init() rather
 * than reimplementing the UI, so branding stays in sync automatically.
 */
export function AssistantWidget({ apiKey, baseUrl, userId }) {
  const initRef = useRef(false);

  useEffect(() => {
    if (initRef.current || !apiKey) return;
    initRef.current = true;

    const origin = (baseUrl || "").replace(/\/+$/, "");
    const src = (origin || "") + "/static/widget.js";

    const doInit = () => {
      if (window.Platform && window.Platform.init) {
        window.Platform.init({ apiKey, baseUrl: origin || undefined, userId });
      }
    };

    if (window.Platform && window.Platform.init) {
      doInit();
      return;
    }

    const existing = document.querySelector(`script[src="${src}"]`);
    if (existing) { existing.addEventListener("load", doInit); return; }

    const s = document.createElement("script");
    s.src = src;
    s.async = true;
    s.addEventListener("load", doInit);
    document.head.appendChild(s);
  }, [apiKey, baseUrl, userId]);

  return null;   // Widget renders itself into document.body via Shadow DOM.
}
