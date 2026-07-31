import { EventSchemas } from "@ag-ui/core";

export const M2D_API_PREFIX = "/api/v1/m2d";

export type DurableState =
  | "ACCEPTED"
  | "RUNNING"
  | "RECOVERABLE"
  | "CANCEL_REQUESTED"
  | "COMPLETED"
  | "NO_MATCH"
  | "FAILED"
  | "ABORTED";

export type RelayState = "HEALTHY" | "DEGRADED";

export interface RunFormValues {
  query: string;
  locale: "zh-CN";
  displayCurrency: string;
  topK: number;
  snapshotVersion: string;
}

export interface M2dStageView {
  name: string;
  state: "RUNNING" | "FINISHED";
  safeCode?: string | null;
}

export interface M2dForkView {
  childId: string;
  depth: number;
  state: "RUNNING" | "COMPLETED" | "FAILED" | "ABORTED";
}

export interface M2dResultView {
  productId: string;
  title: string;
  category: string;
  selectedOffer: {
    market: string;
    currency: string;
    landedCost: string;
  };
  matchedRequirements: readonly string[];
  unknowns: readonly string[];
  reason: string;
  evidenceIds: readonly string[];
}

export interface M2dTerminalView {
  status: "COMPLETED" | "NO_MATCH";
  answerKind: string;
  answer: string;
  results: readonly M2dResultView[];
  evidence: readonly {
    title: string;
    urlDomain: string;
    publishedAt?: string | null;
    snippet: string;
    sourceType: string;
  }[];
  toolSummary: readonly {
    toolName: string;
    callCount: number;
    safeOutcome: string;
  }[];
}

export interface M2dSnapshot {
  schemaVersion: "glodex.m2d.ui-state.v1";
  threadId: string;
  runId: string;
  state: DurableState;
  sourceCursor?: string | null;
  stages: readonly M2dStageView[];
  forks: readonly M2dForkView[];
  terminal?: M2dTerminalView | null;
  relay: {
    state: RelayState;
    safeCode?: string | null;
  };
}

export interface AgUiEvent {
  type: string;
  timestamp: number;
  snapshot?: M2dSnapshot;
  code?: string;
}

export interface StreamEvent {
  event: AgUiEvent;
  sourceCursor: string;
  projectionOrdinal: number;
}

export function createRunAgentInput(values: RunFormValues): Record<string, unknown> {
  const query = values.query.trim();
  if (query.length < 1 || query.length > 2_000) {
    throw new Error("invalid-query");
  }
  const token = createOpaqueToken();
  return {
    threadId: `ui-thread-${token}`,
    runId: `ui-run-${token}`,
    state: {},
    messages: [{ id: `ui-message-${token}`, role: "user", content: query }],
    tools: [],
    context: [],
    forwardedProps: {
      locale: values.locale,
      displayCurrency: values.displayCurrency,
      topK: values.topK,
      snapshotVersion: values.snapshotVersion,
    },
  };
}

export function parseAgUiEvent(value: unknown): AgUiEvent | null {
  const parsed = EventSchemas.safeParse(value);
  if (!parsed.success || !isRecord(parsed.data)) {
    return null;
  }
  const { type, timestamp } = parsed.data;
  if (typeof type !== "string" || typeof timestamp !== "number") {
    return null;
  }
  const allowedTypes = new Set([
    "RUN_STARTED",
    "RUN_FINISHED",
    "RUN_ERROR",
    "STEP_STARTED",
    "STEP_FINISHED",
    "TOOL_CALL_START",
    "TOOL_CALL_END",
    "STATE_SNAPSHOT",
    "CUSTOM",
    "TEXT_MESSAGE_START",
    "TEXT_MESSAGE_CONTENT",
    "TEXT_MESSAGE_END",
  ]);
  if (!allowedTypes.has(type)) {
    return null;
  }
  return parsed.data as AgUiEvent;
}

export function isM2dSnapshot(value: unknown): value is M2dSnapshot {
  if (!isRecord(value) || value.schemaVersion !== "glodex.m2d.ui-state.v1") {
    return false;
  }
  return (
    typeof value.threadId === "string" &&
    typeof value.runId === "string" &&
    typeof value.state === "string" &&
    Array.isArray(value.stages) &&
    Array.isArray(value.forks) &&
    isRecord(value.relay) &&
    typeof value.relay.state === "string"
  );
}

function createOpaqueToken(): string {
  const uuid = globalThis.crypto?.randomUUID?.();
  if (uuid) {
    return uuid.replaceAll("-", "");
  }
  return `${Date.now()}${Math.random().toString(16).slice(2)}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
