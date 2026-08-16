export const WEB_CONSOLE_API_PREFIX = '/api/v1/web-console';
const WEB_CONSOLE_STATE_SCHEMA = ['glodex', 'web-console', 'ui-state', 'v6'].join('.');
const WEB_CONSOLE_EVENT_SCHEMA = ['glodex', 'web-console', 'event', 'v5'].join('.');

export interface ShoppingAgentNeedCoverage {
  label: string;
  importance: 'required' | 'preferred';
  status: 'VERIFIED' | 'UNVERIFIED' | 'MISSING' | 'INSUFFICIENT' | 'CONFLICTED';
  detail: string;
  knownFacts: string[];
  missingEvidence?: string;
  conclusion: string;
  evidence: ShoppingAgentEvidenceFact[];
}

export interface ShoppingAgentEvidenceFact {
  evidenceId: string;
  fieldLabel: string;
  sourceLabel: string;
  sourceUrl: string;
  capturedAt: string;
}

export interface ShoppingAgentResult {
  productId: string;
  title: string;
  categoryLabel: string;
  selectedOffer: {
    sourceLabel: string;
    currency: 'CNY';
    landedCost: string;
  };
  needCoverage: ShoppingAgentNeedCoverage[];
  evidence: ShoppingAgentEvidenceFact[];
}

export interface ShoppingAgentResponse {
  query: string;
  threadId: string;
  runId: string;
  state: 'COMPLETED' | 'NO_MATCH';
  contentKind: 'SHOPPING_RESULTS' | 'CHAT_FALLBACK';
  summary: string;
  results: ShoppingAgentResult[];
  toolSummary: Array<{
    toolName: string;
    callCount: number;
    safeOutcome: string;
  }>;
}

export type ShoppingAgentStageState = 'RUNNING' | 'FINISHED';
export type ShoppingPlatform = 'amazon' | 'shopee' | 'aliexpress' | 'ebay' | 'alibaba' | 'walmart' | 'shein';

export interface ShoppingAgentStage {
  name: string;
  state: ShoppingAgentStageState;
  safeCode?: string;
  toolCallId?: string;
  platforms: ShoppingPlatform[];
  candidateCount?: number;
}

export interface ShoppingAgentFork {
  childId: string;
  state: 'RUNNING' | 'COMPLETED' | 'FAILED' | 'ABORTED';
  platforms: ShoppingPlatform[];
}

export type ShoppingAgentTracePhase = 'THINK' | 'ACT' | 'OBSERVE' | 'REFLECT';
export type ShoppingAgentTraceSource = 'USER_INPUT' | 'TRUSTED_TOOL' | 'VERIFIED_STATE';

export interface ShoppingAgentTraceBullet {
  label: string;
  value: string;
  source: ShoppingAgentTraceSource;
}

export interface ShoppingAgentTraceStep {
  id: string;
  ownerRunId: string;
  phase: ShoppingAgentTracePhase;
  title: string;
  detail: string;
  state: ShoppingAgentStageState;
  bullets: ShoppingAgentTraceBullet[];
  toolName?: string;
  safeCode?: string;
  platforms: ShoppingPlatform[];
  candidateCount?: number;
}

export type ShoppingAgentLifecycleEvent =
  | { type: 'RUN_STARTED'; threadId: string; runId: string }
  | { type: 'RUN_FINISHED'; threadId: string; runId: string }
  | { type: 'RUN_ERROR'; code: string }
  | { type: 'STEP_STARTED'; stepName: string }
  | { type: 'STEP_FINISHED'; stepName: string }
  | { type: 'TOOL_CALL_START'; toolCallId: string; toolCallName: string }
  | { type: 'TOOL_CALL_END'; toolCallId: string }
  | {
      type: 'STATE_SNAPSHOT';
      state: string;
      stages: ShoppingAgentStage[];
      forks: ShoppingAgentFork[];
      trace: ShoppingAgentTraceStep[];
    }
  | { type: 'TEXT_MESSAGE_START'; messageId: string }
  | { type: 'TEXT_MESSAGE_CONTENT'; messageId: string; delta: string }
  | { type: 'TEXT_MESSAGE_END'; messageId: string };

export interface RunShoppingAgentOptions {
  topK?: number;
  threadId?: string;
  attachRunId?: string;
  signal?: AbortSignal;
  onEvent?: (event: ShoppingAgentLifecycleEvent) => void;
}

export class ShoppingAgentRequestError extends Error {
  readonly status: number | null;

  constructor(message: string, status: number | null = null) {
    super(message);
    this.name = 'ShoppingAgentRequestError';
    this.status = status;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function requiredRecord(value: unknown, field: string): Record<string, unknown> {
  if (!isRecord(value)) throw new ShoppingAgentRequestError(`Agent 状态缺少有效的 ${field}。`);
  return value;
}

function requiredString(value: unknown, field: string): string {
  if (typeof value !== 'string' || !value) {
    throw new ShoppingAgentRequestError(`Agent 状态缺少有效的 ${field}。`);
  }
  return value;
}

function requiredCny(value: unknown): 'CNY' {
  if (value !== 'CNY') {
    throw new ShoppingAgentRequestError('Agent 返回了非 CNY 的价格，页面拒绝展示。');
  }
  return value;
}

function requiredArray(value: unknown, field: string): unknown[] {
  if (!Array.isArray(value)) throw new ShoppingAgentRequestError(`Agent 状态缺少有效的 ${field}。`);
  return value;
}

function optionalString(value: unknown, field: string): string | undefined {
  if (value === undefined || value === null) return undefined;
  return requiredString(value, field);
}

function parsePlatforms(value: unknown, field: string): ShoppingPlatform[] {
  const supported = ['amazon', 'shopee', 'aliexpress', 'ebay', 'alibaba', 'walmart', 'shein'];
  return requiredArray(value, field).map(item => {
    const platform = requiredString(item, `${field}[]`);
    if (!supported.includes(platform)) throw new ShoppingAgentRequestError('Agent 返回了无效的平台标识。');
    return platform as ShoppingPlatform;
  });
}

function parseResult(value: unknown): ShoppingAgentResult {
  const result = requiredRecord(value, 'terminal.results');
  const selectedOffer = requiredRecord(result.selectedOffer, 'selectedOffer');
  const coverage = requiredArray(result.needCoverage, 'needCoverage').map(coverageValue => {
    const item = requiredRecord(coverageValue, 'needCoverage[]');
    const importance = requiredString(item.importance, 'importance');
    const status = requiredString(item.status, 'status');
    if (!['required', 'preferred'].includes(importance)) {
      throw new ShoppingAgentRequestError('Agent 返回了无效的需求重要性。');
    }
    if (!['VERIFIED', 'UNVERIFIED', 'MISSING', 'INSUFFICIENT', 'CONFLICTED'].includes(status)) {
      throw new ShoppingAgentRequestError('Agent 返回了无效的需求证据状态。');
    }
    const evidence = parseEvidenceFacts(item.evidence, 'needCoverage[].evidence');
    const knownFacts = requiredArray(item.knownFacts, 'needCoverage[].knownFacts').map(fact =>
      requiredString(fact, 'needCoverage[].knownFacts[]')
    );
    return {
      label: requiredString(item.label, 'label'),
      importance: importance as ShoppingAgentNeedCoverage['importance'],
      status: status as ShoppingAgentNeedCoverage['status'],
      detail: requiredString(item.detail, 'detail'),
      knownFacts,
      missingEvidence: optionalString(item.missingEvidence, 'needCoverage[].missingEvidence'),
      conclusion: requiredString(item.conclusion, 'needCoverage[].conclusion'),
      evidence
    };
  });
  const evidence = parseEvidenceFacts(result.evidence, 'evidence');

  return {
    productId: requiredString(result.productId, 'productId'),
    title: requiredString(result.title, 'title'),
    categoryLabel: requiredString(result.categoryLabel, 'categoryLabel'),
    selectedOffer: {
      sourceLabel: requiredString(selectedOffer.sourceLabel, 'sourceLabel'),
      currency: requiredCny(selectedOffer.currency),
      landedCost: requiredString(selectedOffer.landedCost, 'landedCost')
    },
    needCoverage: coverage,
    evidence
  };
}

function parseEvidenceFacts(value: unknown, field: string): ShoppingAgentEvidenceFact[] {
  return requiredArray(value, field).map(evidenceValue => {
    const evidence = requiredRecord(evidenceValue, `${field}[]`);
    const sourceUrl = requiredString(evidence.sourceUrl, `${field}[].sourceUrl`);
    if (!sourceUrl.startsWith('https://')) {
      throw new ShoppingAgentRequestError('Agent 返回了不安全的证据来源。');
    }
    return {
      evidenceId: requiredString(evidence.evidenceId, `${field}[].evidenceId`),
      fieldLabel: requiredString(evidence.fieldLabel, `${field}[].fieldLabel`),
      sourceLabel: requiredString(evidence.sourceLabel, `${field}[].sourceLabel`),
      sourceUrl,
      capturedAt: requiredString(evidence.capturedAt, `${field}[].capturedAt`)
    };
  });
}

function parseTerminalSnapshot(snapshot: unknown, query: string): ShoppingAgentResponse | null {
  const state = requiredRecord(snapshot, 'snapshot');
  const terminalValue = state.terminal;
  if (terminalValue === null || terminalValue === undefined) return null;

  const terminal = requiredRecord(terminalValue, 'terminal');
  const terminalState = requiredString(state.state, 'state');
  const contentKind = requiredString(terminal.contentKind, 'contentKind');
  if (!['COMPLETED', 'NO_MATCH'].includes(terminalState)) {
    throw new ShoppingAgentRequestError('Agent 终态与结果不一致。');
  }
  if (!['SHOPPING_RESULTS', 'CHAT_FALLBACK'].includes(contentKind)) {
    throw new ShoppingAgentRequestError('Agent 返回了无效的内容类型。');
  }

  return {
    query,
    threadId: requiredString(state.threadId, 'threadId'),
    runId: requiredString(state.runId, 'runId'),
    state: terminalState as ShoppingAgentResponse['state'],
    contentKind: contentKind as ShoppingAgentResponse['contentKind'],
    summary: requiredString(terminal.summary, 'summary'),
    results: requiredArray(terminal.results, 'results').map(parseResult),
    toolSummary: requiredArray(terminal.toolSummary, 'toolSummary').map(value => {
      const item = requiredRecord(value, 'toolSummary[]');
      const callCount = item.callCount;
      if (typeof callCount !== 'number' || !Number.isInteger(callCount) || callCount < 1) {
        throw new ShoppingAgentRequestError('Agent 返回了无效的工具调用计数。');
      }
      return {
        toolName: requiredString(item.toolName, 'toolName'),
        callCount,
        safeOutcome: requiredString(item.safeOutcome, 'safeOutcome')
      };
    })
  };
}

function parseSnapshotState(snapshot: unknown): Extract<ShoppingAgentLifecycleEvent, { type: 'STATE_SNAPSHOT' }> {
  const state = requiredRecord(snapshot, 'snapshot');
  if (state.schemaVersion !== WEB_CONSOLE_STATE_SCHEMA) {
    throw new ShoppingAgentRequestError('Agent 返回了不支持的状态协议版本。');
  }
  const runState = requiredString(state.state, 'snapshot.state');
  if (!['ACCEPTED', 'RUNNING', 'COMPLETED', 'NO_MATCH', 'FAILED', 'ABORTED'].includes(runState)) {
    throw new ShoppingAgentRequestError('Agent 返回了无效的运行状态。');
  }

  const stages = requiredArray(state.stages, 'snapshot.stages').map(stageValue => {
    const stage = requiredRecord(stageValue, 'snapshot.stages[]');
    const stageState = requiredString(stage.state, 'snapshot.stages[].state');
    if (!['RUNNING', 'FINISHED'].includes(stageState)) {
      throw new ShoppingAgentRequestError('Agent 返回了无效的步骤状态。');
    }

    return {
      name: requiredString(stage.name, 'snapshot.stages[].name'),
      state: stageState as ShoppingAgentStageState,
      safeCode: optionalString(stage.safeCode, 'snapshot.stages[].safeCode'),
      toolCallId: optionalString(stage.toolCallId, 'snapshot.stages[].toolCallId'),
      platforms: parsePlatforms(stage.platforms, 'snapshot.stages[].platforms'),
      candidateCount:
        stage.candidateCount === undefined
          ? undefined
          : (() => {
              if (!Number.isInteger(stage.candidateCount) || Number(stage.candidateCount) < 0) {
                throw new ShoppingAgentRequestError('Agent 返回了无效的候选数量。');
              }
              return Number(stage.candidateCount);
            })()
    };
  });

  const forks = requiredArray(state.forks, 'snapshot.forks').map(forkValue => {
    const fork = requiredRecord(forkValue, 'snapshot.forks[]');
    const forkState = requiredString(fork.state, 'snapshot.forks[].state');
    if (!['RUNNING', 'COMPLETED', 'FAILED', 'ABORTED'].includes(forkState)) {
      throw new ShoppingAgentRequestError('Agent 返回了无效的并行任务状态。');
    }
    return {
      childId: requiredString(fork.childId, 'snapshot.forks[].childId'),
      state: forkState as ShoppingAgentFork['state'],
      platforms: parsePlatforms(fork.platforms, 'snapshot.forks[].platforms')
    };
  });

  const trace = requiredArray(state.trace, 'snapshot.trace').map(traceValue => {
    const step = requiredRecord(traceValue, 'snapshot.trace[]');
    const phase = requiredString(step.phase, 'snapshot.trace[].phase');
    const stepState = requiredString(step.state, 'snapshot.trace[].state');
    if (!['THINK', 'ACT', 'OBSERVE', 'REFLECT'].includes(phase)) {
      throw new ShoppingAgentRequestError('Agent 返回了无效的研究阶段。');
    }
    if (!['RUNNING', 'FINISHED'].includes(stepState)) {
      throw new ShoppingAgentRequestError('Agent 返回了无效的研究步骤状态。');
    }
    const bullets = requiredArray(step.bullets, 'snapshot.trace[].bullets').map(bulletValue => {
      const bullet = requiredRecord(bulletValue, 'snapshot.trace[].bullets[]');
      const source = requiredString(bullet.source, 'snapshot.trace[].bullets[].source');
      if (!['USER_INPUT', 'TRUSTED_TOOL', 'VERIFIED_STATE'].includes(source)) {
        throw new ShoppingAgentRequestError('Agent 返回了无效的研究事实来源。');
      }
      return {
        label: requiredString(bullet.label, 'snapshot.trace[].bullets[].label'),
        value: requiredString(bullet.value, 'snapshot.trace[].bullets[].value'),
        source: source as ShoppingAgentTraceSource
      };
    });
    const candidateCount = step.candidateCount;
    if (candidateCount !== undefined && (!Number.isInteger(candidateCount) || Number(candidateCount) < 0)) {
      throw new ShoppingAgentRequestError('Agent 返回了无效的研究候选数量。');
    }
    return {
      id: requiredString(step.id, 'snapshot.trace[].id'),
      ownerRunId: requiredString(step.ownerRunId, 'snapshot.trace[].ownerRunId'),
      phase: phase as ShoppingAgentTracePhase,
      title: requiredString(step.title, 'snapshot.trace[].title'),
      detail: requiredString(step.detail, 'snapshot.trace[].detail'),
      state: stepState as ShoppingAgentStageState,
      bullets,
      toolName: optionalString(step.toolName, 'snapshot.trace[].toolName'),
      safeCode: optionalString(step.safeCode, 'snapshot.trace[].safeCode'),
      platforms: parsePlatforms(step.platforms, 'snapshot.trace[].platforms'),
      candidateCount: candidateCount === undefined ? undefined : Number(candidateCount)
    };
  });

  return { type: 'STATE_SNAPSHOT', state: runState, stages, forks, trace };
}

function parseLifecycleEvent(value: Record<string, unknown>): ShoppingAgentLifecycleEvent | null {
  const type = requiredString(value.type, 'event.type');

  switch (type) {
    case 'RUN_STARTED':
      return {
        type,
        threadId: requiredString(value.threadId, 'event.threadId'),
        runId: requiredString(value.runId, 'event.runId')
      };
    case 'RUN_FINISHED':
      return {
        type,
        threadId: requiredString(value.threadId, 'event.threadId'),
        runId: requiredString(value.runId, 'event.runId')
      };
    case 'RUN_ERROR':
      return { type, code: requiredString(value.code, 'event.code') };
    case 'STEP_STARTED':
    case 'STEP_FINISHED':
      return { type, stepName: requiredString(value.stepName, 'event.stepName') };
    case 'TOOL_CALL_START':
      return {
        type,
        toolCallId: requiredString(value.toolCallId, 'event.toolCallId'),
        toolCallName: requiredString(value.toolCallName, 'event.toolCallName')
      };
    case 'TOOL_CALL_END':
      return { type, toolCallId: requiredString(value.toolCallId, 'event.toolCallId') };
    case 'STATE_SNAPSHOT':
      return parseSnapshotState(value.snapshot);
    case 'TEXT_MESSAGE_START':
      return { type, messageId: requiredString(value.messageId, 'event.messageId') };
    case 'TEXT_MESSAGE_CONTENT':
      return {
        type,
        messageId: requiredString(value.messageId, 'event.messageId'),
        delta: requiredString(value.delta, 'event.delta')
      };
    case 'TEXT_MESSAGE_END':
      return { type, messageId: requiredString(value.messageId, 'event.messageId') };
    case 'CUSTOM':
      {
        const custom = requiredRecord(value.value, 'event.value');
        if (custom.schemaVersion !== WEB_CONSOLE_EVENT_SCHEMA) {
          throw new ShoppingAgentRequestError('Agent 返回了不支持的控制事件协议版本。');
        }
      }
      return null;
    default:
      throw new ShoppingAgentRequestError('Agent 返回了不支持的事件类型。');
  }
}

interface WebSocketStartCommand {
  type: 'START';
  input: Record<string, unknown>;
}

interface WebSocketAttachCommand {
  type: 'ATTACH';
  runId: string;
  afterCursor?: string;
}

type WebSocketCommand = WebSocketStartCommand | WebSocketAttachCommand;

const activeRunSockets = new Map<string, WebSocket>();

function webSocketEndpoint() {
  const endpoint = new URL(`${WEB_CONSOLE_API_PREFIX}/ws`, window.location.href);
  endpoint.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return endpoint.toString();
}

function safeRelayError(code: unknown) {
  if (code === 'WEB_CONSOLE_UPSTREAM_UNAVAILABLE') return '商品研究 Agent 暂不可用，请稍后重试。';
  if (code === 'WEB_CONSOLE_REQUEST_REJECTED') return 'Agent 无法处理这条商品需求，请调整后重试。';
  return 'Agent 实时连接中断，请重新发起研究。';
}

export async function cancelShoppingAgentRun(runId: string): Promise<void> {
  if (!runId) throw new ShoppingAgentRequestError('缺少可取消的 Agent 运行标识。');
  const socket = activeRunSockets.get(runId);
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    throw new ShoppingAgentRequestError('Agent 实时连接已经断开，无法确认取消。');
  }
  socket.send(JSON.stringify({ type: 'CANCEL', runId }));
}

export async function runShoppingAgent(
  query: string,
  { topK = 3, threadId, attachRunId, signal, onEvent }: RunShoppingAgentOptions = {}
): Promise<ShoppingAgentResponse> {
  const normalizedQuery = query.trim().split(/\s+/).join(' ');
  if (!normalizedQuery || normalizedQuery.length > 512) {
    throw new ShoppingAgentRequestError('请输入 1 到 512 个字符的商品需求。');
  }
  if (!Number.isInteger(topK) || topK < 1 || topK > 3) {
    throw new ShoppingAgentRequestError('候选数量必须在 1 到 3 之间。');
  }
  if (threadId !== undefined && !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/.test(threadId)) {
    throw new ShoppingAgentRequestError('会话标识无效。');
  }
  if (attachRunId !== undefined && !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/.test(attachRunId)) {
    throw new ShoppingAgentRequestError('历史运行标识无效。');
  }

  const requestId = crypto.randomUUID();
  const initialCommand: WebSocketCommand = attachRunId
    ? { type: 'ATTACH', runId: attachRunId }
    : {
        type: 'START',
        input: {
          threadId: threadId ?? `thread-${requestId}`,
          runId: `request-${requestId}`,
          state: {},
          messages: [{ id: `message-${requestId}`, role: 'user', content: normalizedQuery }],
          tools: [],
          context: [],
          forwardedProps: {
            locale: 'zh-CN',
            displayCurrency: 'CNY',
            topK,
            snapshotVersion: 'synthetic-interview-commerce-v1'
          }
        }
      };

  return new Promise<ShoppingAgentResponse>((resolve, reject) => {
    let socket: WebSocket | null = null;
    let durableRunId: string | null = null;
    let terminal: ShoppingAgentResponse | null = null;
    let lastCommittedCursor: string | null = null;
    let reconnectAttempted = false;
    let settled = false;
    const receivedPositions = new Set<string>();

    const removeActiveSocket = () => {
      if (durableRunId && activeRunSockets.get(durableRunId) === socket) {
        activeRunSockets.delete(durableRunId);
      }
    };

    const finish = (error?: Error) => {
      if (settled) return;
      settled = true;
      removeActiveSocket();
      signal?.removeEventListener('abort', abortRun);
      if (error) reject(error);
      else if (terminal) resolve(terminal);
      else reject(new ShoppingAgentRequestError('Agent 结束时没有可信结果。'));
    };

    const abortRun = () => {
      socket?.close(1000, 'client-abort');
      finish(new DOMException('The operation was aborted.', 'AbortError'));
    };

    const connect = (command: WebSocketCommand) => {
      socket = new WebSocket(webSocketEndpoint());

      socket.onopen = () => {
        socket?.send(JSON.stringify(command));
      };

      socket.onmessage = message => {
        try {
          const frame = requiredRecord(JSON.parse(String(message.data)), 'WebSocket frame');
          const frameType = requiredString(frame.type, 'frame.type');

          if (frameType === 'READY') {
            const state = requiredRecord(frame.state, 'frame.state');
            durableRunId = requiredString(state.runId, 'frame.state.runId');
            activeRunSockets.set(durableRunId, socket!);
            onEvent?.(parseSnapshotState(state));
            terminal = parseTerminalSnapshot(state, normalizedQuery) ?? terminal;
            if (terminal) {
              socket?.close(1000, 'terminal-snapshot');
              finish();
            }
            return;
          }

          if (frameType === 'EVENT') {
            const cursor = requiredString(frame.sourceCursor, 'frame.sourceCursor');
            const ordinal = frame.projectionOrdinal;
            const count = frame.projectionCount;
            if (
              !Number.isInteger(ordinal) ||
              !Number.isInteger(count) ||
              Number(ordinal) < 0 ||
              Number(count) < 1 ||
              Number(ordinal) >= Number(count)
            ) {
              throw new ShoppingAgentRequestError('Agent 返回了无效的事件位置。');
            }
            const position = `${cursor}:${ordinal}`;
            if (receivedPositions.has(position)) return;
            receivedPositions.add(position);

            const eventValue = requiredRecord(frame.event, 'frame.event');
            const lifecycleEvent = parseLifecycleEvent(eventValue);
            if (lifecycleEvent) onEvent?.(lifecycleEvent);
            if (lifecycleEvent?.type === 'RUN_ERROR') {
              throw new ShoppingAgentRequestError('Agent 无法完成本次研究。');
            }
            if (lifecycleEvent?.type === 'STATE_SNAPSHOT') {
              const trustedTerminal = parseTerminalSnapshot(eventValue.snapshot, normalizedQuery);
              if (trustedTerminal) {
                terminal = trustedTerminal;
                socket?.close(1000, 'terminal-snapshot');
                finish();
                return;
              }
            }
            if (Number(ordinal) === Number(count) - 1) lastCommittedCursor = cursor;
            if (lifecycleEvent?.type === 'RUN_FINISHED') {
              if (!terminal) {
                throw new ShoppingAgentRequestError('Agent 完成事件缺少可信终态结果。');
              }
              socket?.close(1000, 'run-finished');
              finish();
            }
            return;
          }

          if (frameType === 'ERROR') {
            throw new ShoppingAgentRequestError(safeRelayError(frame.code));
          }

          if (frameType === 'CLOSED') {
            finish();
            return;
          }

          throw new ShoppingAgentRequestError('Agent 返回了不支持的 WebSocket 帧。');
        } catch (error) {
          // Browser WebSocket clients may only initiate normal closure (1000)
          // or application-defined closure (3000-4999).  Using the server-only
          // policy code here can throw before ``finish`` records the real
          // projection error, which incorrectly degrades into “no result”.
          socket?.close(4008, 'invalid-frame');
          finish(error instanceof Error ? error : new ShoppingAgentRequestError('Agent 实时连接解析失败。'));
        }
      };

      socket.onclose = () => {
        removeActiveSocket();
        if (settled || signal?.aborted) return;
        if (!reconnectAttempted && durableRunId) {
          reconnectAttempted = true;
          const attach: WebSocketAttachCommand = { type: 'ATTACH', runId: durableRunId };
          if (lastCommittedCursor) attach.afterCursor = lastCommittedCursor;
          connect(attach);
          return;
        }
        finish(new ShoppingAgentRequestError('Agent 实时连接中断，请重新发起研究。'));
      };
    };

    if (signal?.aborted) {
      finish(new DOMException('The operation was aborted.', 'AbortError'));
      return;
    }
    signal?.addEventListener('abort', abortRun, { once: true });
    connect(initialCommand);
  });
}

export function replayShoppingAgentRun(
  query: string,
  runId: string,
  options: Omit<RunShoppingAgentOptions, 'topK' | 'threadId' | 'attachRunId'> = {}
) {
  return runShoppingAgent(query, { ...options, attachRunId: runId });
}
