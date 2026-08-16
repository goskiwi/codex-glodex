import assert from "node:assert/strict";
import { test } from "node:test";
import { createMemory, listConversationThreads, listMemory } from "./memory";

test("memory client uses authenticated web-console endpoints without browser fallback", async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  globalThis.fetch = (async (
    url: string | URL | Request,
    init?: RequestInit,
  ) => {
    calls.push({ url: String(url), init });
    if (String(url).endsWith("/conversation-threads")) {
      return Response.json({
        schemaVersion: "glodex.user-memory.thread-list.v1",
        threads: [],
      });
    }
    if (init?.method === "POST") {
      return Response.json({
        entryId: `mem-${"a".repeat(24)}`,
        category: "blacklist",
        content: "material:plastic",
        revision: 1,
        origin: "manual",
        sourceThreadId: null,
      });
    }
    return Response.json({
      schemaVersion: "glodex.user-memory.memory-list.v3",
      activeEntries: [],
    });
  }) as typeof fetch;

  await listMemory();
  await listConversationThreads();
  await createMemory("blacklist", "material:plastic");

  assert.deepEqual(
    calls.map((call) => call.url),
    [
      "/api/v1/web-console/memory",
      "/api/v1/web-console/conversation-threads",
      "/api/v1/web-console/memory",
    ],
  );
  assert.equal(calls[2]?.init?.credentials, "same-origin");
  assert.equal(
    calls[2]?.init?.body,
    '{"category":"blacklist","content":"material:plastic"}',
  );
});

test("memory client rejects a non-current response", async () => {
  globalThis.fetch = (async () =>
    Response.json({
      schemaVersion: "invalid",
      activeEntries: [],
    })) as typeof fetch;

  await assert.rejects(listMemory(), /响应协议无效/);
});
