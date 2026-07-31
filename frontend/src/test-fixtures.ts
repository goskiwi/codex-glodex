import type { M2dSnapshot } from "./protocol";

export const runningSnapshot: M2dSnapshot = {
  schemaVersion: "glodex.m2d.ui-state.v1",
  threadId: "thread-001",
  runId: "run-001",
  state: "RUNNING",
  sourceCursor: "run-001:2",
  stages: [{ name: "tool:item_search", state: "RUNNING" }],
  forks: [],
  terminal: null,
  relay: { state: "HEALTHY", safeCode: null },
};

export const completedSnapshot: M2dSnapshot = {
  ...runningSnapshot,
  state: "COMPLETED",
  sourceCursor: "run-001:8",
  stages: [{ name: "tool:item_search", state: "FINISHED", safeCode: "OK" }],
  terminal: {
    status: "COMPLETED",
    answerKind: "RECOMMENDATION",
    answer: "已发布的安全结果。",
    results: [
      {
        productId: "product-001",
        title: "Travel Notebook",
        category: "Laptop",
        selectedOffer: { market: "demo-market", currency: "USD", landedCost: "799.00" },
        matchedRequirements: ["portable"],
        unknowns: [],
        reason: "Eligible result",
        evidenceIds: ["evidence-001"],
      },
    ],
    evidence: [],
    toolSummary: [{ toolName: "item_search", callCount: 1, safeOutcome: "OK" }],
  },
};
