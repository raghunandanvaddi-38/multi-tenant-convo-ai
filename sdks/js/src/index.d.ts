/**
 * AI Platform SDK — TypeScript definitions.
 * Runtime is plain JavaScript in ./index.js; types live here.
 */

export interface PlatformOptions {
  apiKey: string;
  baseUrl?: string;
  userId?: string;
  fetchImpl?: typeof fetch;
}

export interface ChatOptions {
  conversationId?: string;
  userId?: string;
}

export interface ChatReply {
  reply: string;
  conversation_id: string;
  workspace_id: string;
  latency_ms: number;
}

export interface WorkspaceInfo {
  workspace_id: string;
  name: string;
  branding: Record<string, any>;
}

export interface DocumentInfo {
  id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  status: "queued" | "processing" | "ready" | "failed";
  chunk_count: number;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface AnalyticsSummary {
  days: number;
  total_messages: number;
  unique_conversations: number;
  total_tokens_out: number;
  avg_latency_ms: number;
  errors: number;
  top_queries: { query: string; count: number }[];
}

export interface StreamCallbacks {
  onToken?: (text: string) => void;
  onSession?: (info: { conversation_id: string }) => void;
  onDone?: (info: { latency_ms: number }) => void;
  onError?: (msg: string) => void;
}

export interface WSCallbacks extends StreamCallbacks {
  onOpen?: (info: { conversation_id: string; workspace_id: string; branding: any }) => void;
  onClose?: (info: { code: number; reason: string }) => void;
}

export interface WSHandle {
  send: (text: string) => void;
  close: () => void;
}

export class PlatformClient {
  constructor(opts: PlatformOptions);

  workspace(): Promise<WorkspaceInfo>;

  chat(message: string, opts?: ChatOptions): Promise<ChatReply>;
  chatStream(message: string, opts: ChatOptions & StreamCallbacks): Promise<void>;
  chatSocket(opts: ChatOptions & WSCallbacks): WSHandle;

  listDocuments(): Promise<DocumentInfo[]>;
  uploadDocument(file: File | Blob, filename?: string): Promise<DocumentInfo>;
  deleteDocument(id: string): Promise<void>;

  analyticsSummary(days?: number): Promise<AnalyticsSummary>;
}
