// Client for the muse-linux native API (see src/muselinux/server.py).
// Same origin when the backend serves the GUI; inside the Tauri
// shell the origin is tauri://, so call the local backend directly.

export const API_BASE =
  window.location.protocol.startsWith("tauri") ||
  window.location.hostname.endsWith("tauri.localhost")
    ? "http://127.0.0.1:8770"
    : "";

export interface ChatInfo {
  id: string;
  label: string;
  session_id: string | null;
  is_main: boolean;
}

export interface HistoryMessage {
  role: "user" | "assistant";
  text: string;
  message_id: string | null;
  occurred_at_ms: number | null;
  reaction: string | null;
}

export interface PendingApproval {
  id: string | null;
  title: string;
  detail: string;
}

export interface StatusInfo {
  paired?: boolean;
  vm_fetch_status?: number | null;
  vm_count?: number;
  node_id?: string;
  error?: string;
}

export type StreamEvent =
  | { type: "status"; code: string }
  | { type: "reply"; text: string }
  | { type: "approvals"; pending: PendingApproval[] }
  | {
      type: "done";
      completion: string;
      task_status: string | null;
      ok: boolean;
    }
  | { type: "error"; error: string };

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    throw new Error(`${path}: HTTP ${response.status}`);
  }
  return (await response.json()) as T;
}

export const api = {
  status: () => json<StatusInfo>(`${API_BASE}/api/status`),
  chats: () => json<{ chats: ChatInfo[] }>(`${API_BASE}/api/chats`),
  createChat: (label: string) =>
    json<ChatInfo>(`${API_BASE}/api/chats`, {
      method: "POST",
      body: JSON.stringify({ label }),
    }),
  addChat: (sessionId: string, label: string) =>
    json<ChatInfo>(`${API_BASE}/api/chats/add`, {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId, label }),
    }),
  renameChat: (id: string, label: string) =>
    json<ChatInfo>(`${API_BASE}/api/chats/${encodeURIComponent(id)}/rename`, {
      method: "POST",
      body: JSON.stringify({ label }),
    }),
  history: (id: string, limit = 50) =>
    json<{ messages: HistoryMessage[]; has_more: boolean }>(
      `${API_BASE}/api/chats/${encodeURIComponent(id)}/history?limit=${limit}`,
    ),
};

/** Fetch the spoken version of one assistant message (MP3). */
export async function fetchTts(
  chatId: string,
  messageId: string,
): Promise<Blob> {
  const response = await fetch(
    `${API_BASE}/api/chats/${encodeURIComponent(chatId)}/tts/` +
      encodeURIComponent(messageId),
  );
  if (!response.ok) {
    throw new Error(`tts: HTTP ${response.status}`);
  }
  return response.blob();
}

/** Read an SSE-over-fetch response body as a stream of turn events. */
async function* parseEventStream(
  response: Response,
): AsyncGenerator<StreamEvent> {
  if (!response.ok || !response.body) {
    throw new Error(`stream: HTTP ${response.status}`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut = buffer.indexOf("\n\n");
    while (cut >= 0) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      const line = block.split("\n").find((l) => l.startsWith("data: "));
      if (line) {
        try {
          yield JSON.parse(line.slice(6)) as StreamEvent;
        } catch {
          // keepalive or partial frame; ignore
        }
      }
      cut = buffer.indexOf("\n\n");
    }
  }
}

/** POST a message and stream the turn's events (SSE over fetch). */
export async function* streamMessage(
  chatId: string,
  text: string,
  signal: AbortSignal,
): AsyncGenerator<StreamEvent> {
  const response = await fetch(
    `${API_BASE}/api/chats/${encodeURIComponent(chatId)}/messages`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
      signal,
    },
  );
  yield* parseEventStream(response);
}

/** POST a voice message (WAV bytes) and stream the turn's events. */
export async function* streamVoice(
  chatId: string,
  wav: ArrayBuffer,
  signal: AbortSignal,
): AsyncGenerator<StreamEvent> {
  const response = await fetch(
    `${API_BASE}/api/chats/${encodeURIComponent(chatId)}/voice`,
    {
      method: "POST",
      headers: { "Content-Type": "audio/wav" },
      body: wav,
      signal,
    },
  );
  yield* parseEventStream(response);
}
