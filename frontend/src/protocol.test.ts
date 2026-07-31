import { describe, expect, it } from "vitest";

import { createRunAgentInput, parseAgUiEvent } from "./protocol";

describe("M2d AG-UI protocol helpers", () => {
  it("encodes only the approved RunAgentInput subset", () => {
    const input = createRunAgentInput({
      query: "  推荐轻薄本  ",
      locale: "zh-CN",
      displayCurrency: "USD",
      topK: 3,
      snapshotVersion: "m1d-demo-v1",
    });

    expect(Object.keys(input).sort()).toEqual([
      "context",
      "forwardedProps",
      "messages",
      "runId",
      "state",
      "threadId",
      "tools",
    ]);
    expect(input.messages).toEqual([
      expect.objectContaining({ role: "user", content: "推荐轻薄本" }),
    ]);
  });

  it("uses @ag-ui/core validation before accepting a fixed event", () => {
    expect(
      parseAgUiEvent({ type: "RUN_STARTED", timestamp: 1, threadId: "thread-001", runId: "run-001" }),
    ).toMatchObject({ type: "RUN_STARTED", runId: "run-001" });
    expect(parseAgUiEvent({ type: "RAW", timestamp: 1, event: "leak" })).toBeNull();
    expect(parseAgUiEvent({ type: "RUN_STARTED", timestamp: "not-a-number" })).toBeNull();
  });
});
