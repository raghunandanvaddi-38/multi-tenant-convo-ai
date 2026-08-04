import type { ChatOptions, ChatReply, PlatformClient } from "@platform/sdk";
import type { ReactNode } from "react";

export interface Message {
  role: "user" | "assistant";
  text: string;
}

export interface UseChatState {
  messages: Message[];
  isStreaming: boolean;
  error: string | null;
  conversationId: string | null;
  send: (text: string) => Promise<void>;
  reset: () => void;
}

export function useChat(client: PlatformClient, opts?: ChatOptions): UseChatState;

export interface AssistantWidgetProps {
  apiKey: string;
  baseUrl?: string;
  userId?: string;
  buttonLabel?: string;
  children?: ReactNode;
}

export function AssistantWidget(props: AssistantWidgetProps): JSX.Element;
