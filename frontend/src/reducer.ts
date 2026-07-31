import type { M2dSnapshot, StreamEvent } from "./protocol";
import { isM2dSnapshot } from "./protocol";

export type StreamStatus = "EMPTY" | "CONNECTING" | "STREAMING" | "DEGRADED" | "UNAVAILABLE";

export interface ConsoleState {
  snapshot: M2dSnapshot | null;
  status: StreamStatus;
  safeCode: string | null;
  seenProjectionKeys: ReadonlySet<string>;
}

export const initialConsoleState: ConsoleState = {
  snapshot: null,
  status: "EMPTY",
  safeCode: null,
  seenProjectionKeys: new Set<string>(),
};

export type ConsoleAction =
  | { type: "RESET" }
  | { type: "CONNECTING" }
  | { type: "EVENT"; streamEvent: StreamEvent }
  | { type: "SNAPSHOT"; snapshot: M2dSnapshot }
  | { type: "UNAVAILABLE" };

export function consoleReducer(state: ConsoleState, action: ConsoleAction): ConsoleState {
  switch (action.type) {
    case "RESET":
      return initialConsoleState;
    case "CONNECTING":
      return { ...state, status: "CONNECTING", safeCode: null };
    case "SNAPSHOT":
      return stateFromSnapshot(state, action.snapshot);
    case "UNAVAILABLE":
      return { ...state, status: "UNAVAILABLE", safeCode: "M2D_UPSTREAM_UNAVAILABLE" };
    case "EVENT":
      return reduceStreamEvent(state, action.streamEvent);
  }
}

function reduceStreamEvent(state: ConsoleState, streamEvent: StreamEvent): ConsoleState {
  const key = `${streamEvent.sourceCursor}/${streamEvent.projectionOrdinal}`;
  if (state.seenProjectionKeys.has(key)) {
    return state;
  }
  const seenProjectionKeys = new Set(state.seenProjectionKeys);
  seenProjectionKeys.add(key);
  const { event } = streamEvent;
  if (event.type === "STATE_SNAPSHOT" && isM2dSnapshot(event.snapshot)) {
    return stateFromSnapshot({ ...state, seenProjectionKeys }, event.snapshot);
  }
  if (event.type === "RUN_ERROR") {
    const safeCode = typeof event.code === "string" ? event.code : "M2D_PROJECTION_INVALID";
    if (!safeCode.startsWith("M2D_")) {
      return {
        ...state,
        safeCode,
        seenProjectionKeys,
      };
    }
    return {
      ...state,
      status: "DEGRADED",
      safeCode,
      seenProjectionKeys,
    };
  }
  return { ...state, status: "STREAMING", seenProjectionKeys };
}

function stateFromSnapshot(state: ConsoleState, snapshot: M2dSnapshot): ConsoleState {
  const degraded = snapshot.relay.state === "DEGRADED";
  return {
    ...state,
    snapshot,
    status: degraded ? "DEGRADED" : "STREAMING",
    safeCode: degraded ? snapshot.relay.safeCode ?? "M2D_STREAM_INTERRUPTED" : null,
  };
}
