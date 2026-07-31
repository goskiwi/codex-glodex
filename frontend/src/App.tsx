import { type FormEvent, useReducer, useRef, useState } from "react";

import {
  createRunAgentInput,
  isM2dSnapshot,
  M2D_API_PREFIX,
  type M2dSnapshot,
  type RunFormValues,
} from "./protocol";
import { consoleReducer, initialConsoleState } from "./reducer";
import { consumeAgUiStream, M2dStreamError } from "./sse";

const DEFAULT_FORM: RunFormValues = {
  query: "",
  locale: "zh-CN",
  displayCurrency: "USD",
  topK: 3,
  snapshotVersion: "m1d-demo-v1",
};

const ACTIVE_STATES = new Set(["ACCEPTED", "RUNNING"]);

export function App(): React.JSX.Element {
  const [form, setForm] = useState<RunFormValues>(DEFAULT_FORM);
  const [formError, setFormError] = useState<string | null>(null);
  const [consoleState, dispatch] = useReducer(consoleReducer, initialConsoleState);
  const streamAbortController = useRef<AbortController | null>(null);
  const snapshot = consoleState.snapshot;

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setFormError(null);
    let payload: Record<string, unknown>;
    try {
      payload = createRunAgentInput(form);
    } catch {
      setFormError("请输入 1–2000 个字符的购物需求。");
      return;
    }

    streamAbortController.current?.abort();
    const controller = new AbortController();
    streamAbortController.current = controller;
    dispatch({ type: "RESET" });
    dispatch({ type: "CONNECTING" });
    try {
      const response = await fetch(`${M2D_API_PREFIX}/ag-ui`, {
        method: "POST",
        headers: {
          accept: "text/event-stream",
          "content-type": "application/json",
        },
        body: JSON.stringify(payload),
        signal: controller.signal,
      });
      await consumeAgUiStream(response, (streamEvent) => {
        dispatch({ type: "EVENT", streamEvent });
      });
    } catch (error) {
      if (controller.signal.aborted) {
        return;
      }
      if (error instanceof M2dStreamError && error.safeCode === "M2D_PROJECTION_INVALID") {
        dispatch({
          type: "EVENT",
          streamEvent: {
            event: { type: "RUN_ERROR", timestamp: 0, code: error.safeCode },
            sourceCursor: "m2d-client",
            projectionOrdinal: 0,
          },
        });
      } else {
        dispatch({ type: "UNAVAILABLE" });
      }
    }
  }

  async function applyControl(action: "cancel" | "resume"): Promise<void> {
    if (!snapshot) {
      return;
    }
    try {
      const response = await fetch(
        `${M2D_API_PREFIX}/runs/${encodeURIComponent(snapshot.runId)}/${action}`,
        { method: "POST", headers: { accept: "application/json" } },
      );
      const value: unknown = await response.json();
      if (!response.ok || !isM2dSnapshot(value)) {
        dispatch({ type: "UNAVAILABLE" });
        return;
      }
      dispatch({ type: "SNAPSHOT", snapshot: value });
    } catch {
      dispatch({ type: "UNAVAILABLE" });
    }
  }

  async function reconnect(): Promise<void> {
    if (!snapshot || consoleState.status !== "DEGRADED") {
      return;
    }
    const controller = new AbortController();
    streamAbortController.current?.abort();
    streamAbortController.current = controller;
    dispatch({ type: "CONNECTING" });
    try {
      const response = await fetch(
        `${M2D_API_PREFIX}/runs/${encodeURIComponent(snapshot.runId)}/events`,
        {
          headers: {
            accept: "text/event-stream",
            ...(snapshot.sourceCursor ? { "last-event-id": snapshot.sourceCursor } : {}),
          },
          signal: controller.signal,
        },
      );
      await consumeAgUiStream(response, (streamEvent) => {
        dispatch({ type: "EVENT", streamEvent });
      });
    } catch (error) {
      if (!controller.signal.aborted && !(error instanceof M2dStreamError)) {
        dispatch({ type: "UNAVAILABLE" });
      }
    }
  }

  function newRun(): void {
    streamAbortController.current?.abort();
    streamAbortController.current = null;
    setForm(DEFAULT_FORM);
    setFormError(null);
    dispatch({ type: "RESET" });
  }

  const canCancel = snapshot !== null && ACTIVE_STATES.has(snapshot.state);
  const canResume = snapshot?.state === "RECOVERABLE";
  const canReconnect = snapshot !== null && consoleState.status === "DEGRADED";

  return (
    <main className="console-shell">
      <header className="masthead">
        <div>
          <p className="eyebrow">GLODEX / M2D</p>
          <h1>Run Console</h1>
        </div>
        <p className="header-note">安全 AG-UI 投影 · 本机 durable run</p>
      </header>

      <section className="panel request-panel" aria-labelledby="request-title">
        <div className="panel-heading">
          <div>
            <p className="eyebrow">NEW REQUEST</p>
            <h2 id="request-title">提交一个购物请求</h2>
          </div>
          <button className="button secondary" type="button" onClick={newRun}>
            New run
          </button>
        </div>
        <form onSubmit={submit}>
          <label>
            Shopping request
            <textarea
              value={form.query}
              maxLength={2_000}
              rows={3}
              placeholder="例如：推荐 800 美元以内、适合出差的轻薄本"
              onChange={(event) => setForm({ ...form, query: event.target.value })}
            />
          </label>
          <div className="form-grid">
            <label>
              Locale
              <select
                value={form.locale}
                onChange={(event) => setForm({ ...form, locale: event.target.value as "zh-CN" })}
              >
                <option value="zh-CN">zh-CN</option>
              </select>
            </label>
            <label>
              Currency
              <select
                value={form.displayCurrency}
                onChange={(event) => setForm({ ...form, displayCurrency: event.target.value })}
              >
                <option value="USD">USD</option>
                <option value="CNY">CNY</option>
              </select>
            </label>
            <label>
              Top results
              <select
                value={form.topK}
                onChange={(event) => setForm({ ...form, topK: Number(event.target.value) })}
              >
                <option value={1}>1</option>
                <option value={2}>2</option>
                <option value={3}>3</option>
              </select>
            </label>
            <label>
              Snapshot
              <input
                value={form.snapshotVersion}
                maxLength={64}
                onChange={(event) => setForm({ ...form, snapshotVersion: event.target.value })}
              />
            </label>
          </div>
          {formError ? <p className="form-error">{formError}</p> : null}
          <button className="button primary" type="submit">
            Start durable run
          </button>
        </form>
      </section>

      <section className="panel run-panel" aria-labelledby="run-title">
        <div className="panel-heading">
          <div>
            <p className="eyebrow">LIVE RUN</p>
            <h2 id="run-title">安全运行状态</h2>
          </div>
          <StatusBadge status={snapshot?.state ?? consoleState.status} />
        </div>
        {snapshot ? (
          <RunView snapshot={snapshot} safeCode={consoleState.safeCode} />
        ) : (
          <p className="empty-state">
            {consoleState.status === "CONNECTING"
              ? "正在连接本机 M2d adapter…"
              : consoleState.status === "UNAVAILABLE"
                ? "本机 M2d 服务不可用。请确认 8767 服务已启动。"
                : consoleState.status === "DEGRADED"
                  ? `安全 relay 已停止：${consoleState.safeCode ?? "M2D_STREAM_INTERRUPTED"}`
                : "提交请求后，这里会显示 durable run 的安全投影。"}
          </p>
        )}
        <div className="controls" aria-label="run controls">
          <button className="button secondary" type="button" disabled={!canCancel} onClick={() => void applyControl("cancel")}>
            Cancel
          </button>
          <button className="button secondary" type="button" disabled={!canResume} onClick={() => void applyControl("resume")}>
            Resume
          </button>
          <button className="button secondary" type="button" disabled={!canReconnect} onClick={() => void reconnect()}>
            Reconnect
          </button>
        </div>
      </section>

      <TerminalView snapshot={snapshot} safeCode={consoleState.safeCode} />
    </main>
  );
}

function StatusBadge({ status }: { status: string }): React.JSX.Element {
  return <span className={`status-badge status-${status.toLowerCase()}`}>{status}</span>;
}

function RunView({ snapshot, safeCode }: { snapshot: M2dSnapshot; safeCode: string | null }): React.JSX.Element {
  return (
    <div className="run-view">
      <dl className="run-meta">
        <div>
          <dt>Run</dt>
          <dd>{snapshot.runId}</dd>
        </div>
        <div>
          <dt>Source cursor</dt>
          <dd>{snapshot.sourceCursor ?? "Waiting for durable event"}</dd>
        </div>
      </dl>
      {safeCode ? <p className="safe-code">{safeCode}</p> : null}
      <div className="timeline" aria-label="safe run timeline">
        {snapshot.stages.length === 0 ? (
          <p className="empty-state">等待 durable lifecycle event…</p>
        ) : (
          snapshot.stages.map((stage, index) => (
            <div className="timeline-item" key={`${stage.name}-${index}`}>
              <span className={`timeline-marker ${stage.state.toLowerCase()}`} />
              <div>
                <strong>{stage.name}</strong>
                <span>{stage.state}</span>
                {stage.safeCode ? <span>{stage.safeCode}</span> : null}
              </div>
            </div>
          ))
        )}
      </div>
      {snapshot.forks.length > 0 ? (
        <div className="forks" aria-label="subtasks">
          <h3>Subtasks</h3>
          {snapshot.forks.map((fork) => (
            <p key={fork.childId}>
              depth {fork.depth} · {fork.childId} · {fork.state}
            </p>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function TerminalView({
  snapshot,
  safeCode,
}: {
  snapshot: M2dSnapshot | null;
  safeCode: string | null;
}): React.JSX.Element | null {
  if (!snapshot) {
    return null;
  }
  if (snapshot.state === "FAILED" || snapshot.state === "ABORTED") {
    return (
      <section className="panel terminal-panel" aria-live="polite">
        <p className="eyebrow">TERMINAL</p>
        <h2>Run could not be completed.</h2>
        <p className="safe-code">{safeCode ?? "M2D_PROJECTION_INVALID"}</p>
      </section>
    );
  }
  if (!snapshot.terminal) {
    return null;
  }
  const terminal = snapshot.terminal;
  return (
    <section className="panel terminal-panel" aria-live="polite">
      <p className="eyebrow">TRUSTED RESULT</p>
      <h2>{terminal.status === "NO_MATCH" ? "No matching result" : "Published result"}</h2>
      <p className="answer">{terminal.answer}</p>
      {terminal.results.length > 0 ? (
        <div className="result-grid">
          {terminal.results.map((result) => (
            <article className="result-card" key={result.productId}>
              <p className="eyebrow">{result.category}</p>
              <h3>{result.title}</h3>
              <p>
                {result.selectedOffer.market} · {result.selectedOffer.landedCost} {result.selectedOffer.currency}
              </p>
              <p>{result.reason}</p>
              {result.matchedRequirements.length > 0 ? (
                <p className="fact-line">Matched: {result.matchedRequirements.join(" · ")}</p>
              ) : null}
              {result.unknowns.length > 0 ? <p className="fact-line">Unknown: {result.unknowns.join(" · ")}</p> : null}
              {result.evidenceIds.length > 0 ? <p className="fact-line">Evidence: {result.evidenceIds.join(" · ")}</p> : null}
            </article>
          ))}
        </div>
      ) : null}
      {terminal.toolSummary.length > 0 ? (
        <p className="tool-summary">
          Tool summary: {terminal.toolSummary.map((tool) => `${tool.toolName} ×${tool.callCount}`).join(" · ")}
        </p>
      ) : null}
    </section>
  );
}
