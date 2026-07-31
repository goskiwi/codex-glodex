import type { StreamEvent } from "./protocol";
import { parseAgUiEvent } from "./protocol";

export class M2dStreamError extends Error {
  readonly safeCode: "M2D_UPSTREAM_UNAVAILABLE" | "M2D_PROJECTION_INVALID";

  constructor(safeCode: "M2D_UPSTREAM_UNAVAILABLE" | "M2D_PROJECTION_INVALID") {
    super(safeCode);
    this.safeCode = safeCode;
  }
}

export async function consumeAgUiStream(
  response: Response,
  onEvent: (event: StreamEvent) => void,
): Promise<void> {
  if (!response.ok || !response.body || !isEventStream(response)) {
    throw new M2dStreamError("M2D_UPSTREAM_UNAVAILABLE");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffered = "";
  let previousCursor = "";
  let projectionOrdinal = 0;

  while (true) {
    const chunk = await reader.read();
    if (chunk.done) {
      break;
    }
    buffered += decoder.decode(chunk.value, { stream: true }).replaceAll("\r\n", "\n");
    const frames = buffered.split("\n\n");
    buffered = frames.pop() ?? "";
    for (const frame of frames) {
      const parsed = parseSseFrame(frame);
      if (parsed === null) {
        continue;
      }
      const event = parseAgUiEvent(parsed.data);
      if (event === null) {
        throw new M2dStreamError("M2D_PROJECTION_INVALID");
      }
      const sourceCursor = parsed.id || "m2d-relay";
      projectionOrdinal = sourceCursor === previousCursor ? projectionOrdinal + 1 : 0;
      previousCursor = sourceCursor;
      onEvent({ event, sourceCursor, projectionOrdinal });
    }
  }
}

function isEventStream(response: Response): boolean {
  return response.headers.get("content-type")?.toLowerCase().includes("text/event-stream") ?? false;
}

function parseSseFrame(frame: string): { id: string; data: unknown } | null {
  let id = "";
  const data: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("id:")) {
      id = line.slice(3).trim();
    } else if (line.startsWith("data:")) {
      data.push(line.slice(5).trimStart());
    }
  }
  if (data.length === 0) {
    return null;
  }
  try {
    return { id, data: JSON.parse(data.join("\n")) as unknown };
  } catch {
    throw new M2dStreamError("M2D_PROJECTION_INVALID");
  }
}
