import { describe, expect, it } from "vitest";

import { consoleReducer, initialConsoleState } from "./reducer";
import { completedSnapshot, runningSnapshot } from "./test-fixtures";

describe("consoleReducer", () => {
  it("deduplicates one source cursor and renders the trusted terminal snapshot once", () => {
    const action = {
      type: "EVENT" as const,
      streamEvent: {
        event: { type: "STATE_SNAPSHOT", timestamp: 8, snapshot: completedSnapshot },
        sourceCursor: "run-001:8",
        projectionOrdinal: 0,
      },
    };
    const first = consoleReducer(initialConsoleState, action);
    const repeated = consoleReducer(first, action);

    expect(first.snapshot?.terminal?.answer).toBe("已发布的安全结果。");
    expect(repeated).toBe(first);
  });

  it("clears business terminal content when a failed durable snapshot arrives", () => {
    const completed = consoleReducer(initialConsoleState, {
      type: "SNAPSHOT",
      snapshot: completedSnapshot,
    });
    const failed = consoleReducer(completed, {
      type: "SNAPSHOT",
      snapshot: { ...runningSnapshot, state: "FAILED", terminal: null },
    });

    expect(failed.snapshot?.terminal).toBeNull();
    expect(failed.snapshot?.state).toBe("FAILED");
  });

  it("keeps a durable failed run distinct from an M2d relay failure", () => {
    const failed = consoleReducer(initialConsoleState, {
      type: "SNAPSHOT",
      snapshot: { ...runningSnapshot, state: "FAILED", terminal: null },
    });
    const afterError = consoleReducer(failed, {
      type: "EVENT",
      streamEvent: {
        event: { type: "RUN_ERROR", timestamp: 3, code: "INVALID_ACTION" },
        sourceCursor: "run-001:3",
        projectionOrdinal: 0,
      },
    });

    expect(afterError.status).toBe("STREAMING");
    expect(afterError.safeCode).toBe("INVALID_ACTION");
  });
});
