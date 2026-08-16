const MEMORY_API_PREFIX = "/api/v1/web-console";

export type MemoryCategory = "preference" | "blacklist" | "history";
export type MemoryOrigin = "manual" | "explicit_reflect";

export interface MemoryEntry {
  entryId: string;
  category: MemoryCategory;
  content: string;
  revision: number;
  origin: MemoryOrigin;
  sourceThreadId: string | null;
}

export interface ConversationThread {
  threadId: string;
  turnCount: number;
}

export interface ConversationTurn {
  ordinal: number;
  role: "user" | "assistant";
  content: string;
  terminalRunId: string | null;
}

interface MemoryListResponse {
  schemaVersion: "glodex.user-memory.memory-list.v3";
  activeEntries: MemoryEntry[];
}

interface ThreadListResponse {
  schemaVersion: "glodex.user-memory.thread-list.v1";
  threads: ConversationThread[];
}

interface TurnPageResponse {
  schemaVersion: "glodex.user-memory.turn-page.v1";
  threadId: string;
  turns: ConversationTurn[];
  nextBeforeOrdinal: number | null;
}

function parseMemoryList(value: unknown): MemoryListResponse {
  if (
    typeof value !== "object" ||
    value === null ||
    (value as { schemaVersion?: unknown }).schemaVersion !==
      "glodex.user-memory.memory-list.v3" ||
    !Array.isArray((value as { activeEntries?: unknown }).activeEntries)
  ) {
    throw new Error("长期记忆响应协议无效。");
  }
  return value as MemoryListResponse;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${MEMORY_API_PREFIX}${path}`, {
    credentials: "same-origin",
    headers: {
      accept: "application/json",
      ...(init?.body ? { "content-type": "application/json" } : {}),
    },
    ...init,
  });
  if (!response.ok) {
    let message = "长期记忆服务暂时不可用。";
    try {
      const body = (await response.json()) as { detail?: string };
      if (typeof body.detail === "string" && body.detail) message = body.detail;
    } catch {
      // The status is enough when the server deliberately returns no JSON body.
    }
    throw new Error(message);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function listMemory() {
  return parseMemoryList(await request<unknown>("/memory"));
}

export function createMemory(category: MemoryCategory, content: string) {
  return request<MemoryEntry>("/memory", {
    method: "POST",
    body: JSON.stringify({ category, content }),
  });
}

export function replaceMemory(
  entryId: string,
  category: MemoryCategory,
  content: string,
) {
  return request<MemoryEntry>(`/memory/${encodeURIComponent(entryId)}`, {
    method: "PUT",
    body: JSON.stringify({ category, content }),
  });
}

export async function deleteMemory(entryId: string) {
  await request<unknown>(`/memory/${encodeURIComponent(entryId)}`, {
    method: "DELETE",
  });
}

export function listConversationThreads() {
  return request<ThreadListResponse>("/conversation-threads");
}

export function listConversationTurns(threadId: string) {
  return request<TurnPageResponse>(
    `/conversation-threads/${encodeURIComponent(threadId)}/turns`,
  );
}

export async function deleteConversationThread(threadId: string) {
  await request<unknown>(
    `/conversation-threads/${encodeURIComponent(threadId)}`,
    { method: "DELETE" },
  );
}
