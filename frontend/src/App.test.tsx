import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { completedSnapshot } from "./test-fixtures";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Glodex Run Console", () => {
  it("submits to the same-origin M2d AG-UI route and renders safe terminal facts", async () => {
    const event = JSON.stringify({
      type: "STATE_SNAPSHOT",
      timestamp: 8,
      snapshot: completedSnapshot,
    });
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(`id: run-001:8\ndata: ${event}\n\n`, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    fireEvent.change(screen.getByLabelText("Shopping request"), {
      target: { value: "推荐轻薄本" },
    });
    fireEvent.submit(screen.getByRole("button", { name: "Start durable run" }).closest("form")!);

    expect(await screen.findByText("Published result")).toBeTruthy();
    expect(screen.getByText("Travel Notebook")).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/m2d/ag-ui",
      expect.objectContaining({ method: "POST" }),
    );
  });
});
