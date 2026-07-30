"use strict";

const BUSINESS_TOOLS = [
  ["planner", "规划器"],
  ["chat_fallback", "聊天兜底"],
  ["web_search", "网页证据检索"],
  ["category_insight", "品类洞察"],
  ["item_search", "商品检索"],
  ["item_picker", "候选选择"],
  ["price_compare", "价格比较"],
  ["shipping_calc", "运费估算"],
  ["shopping_summary", "购物摘要"],
];
const META_TOOL = ["dispatch_tool", "dispatch_tool（子 Agent 元工具）"];
const EVENT_LABELS = {
  AGENT_STARTED: "Agent 回放开始",
  MODEL_STARTED: "模型选择下一步",
  MODEL_FINISHED: "模型选择工具",
  TOOL_STARTED: "工具开始执行",
  TOOL_FINISHED: "工具完成",
  FORK_STARTED: "子 Agent 分支开始",
  FORK_FINISHED: "子 Agent 分支完成",
  AGENT_RESULT: "Agent 回放完成",
  AGENT_ERROR: "Agent 回放安全终止",
};
const METRICS = [
  ["exact_at_10", "Exact@10"],
  ["mrr_at_10", "MRR@10"],
  ["ndcg_at_10", "nDCG@10"],
];
const STEP_DELAY_MS = 650;

const elements = {
  benchmarkStatus: document.querySelector("#benchmark-status"),
  eventTimeline: document.querySelector("#event-timeline"),
  metricCards: document.querySelector("#metric-cards"),
  pause: document.querySelector("#pause-button"),
  play: document.querySelector("#play-button"),
  provenance: document.querySelector("#benchmark-provenance"),
  replayStatus: document.querySelector("#replay-status"),
  reset: document.querySelector("#reset-button"),
  toolInventory: document.querySelector("#tool-inventory"),
};

const replay = {
  cursor: 0,
  events: [],
  status: "idle",
  timer: null,
};

function setStatus(element, text, tone = "") {
  element.textContent = text;
  element.dataset.tone = tone;
}

function renderTools(events) {
  const executed = new Set(events.map((event) => event.toolName).filter(Boolean));
  const tools = [...BUSINESS_TOOLS, META_TOOL];
  elements.toolInventory.replaceChildren(
    ...tools.map(([name, label], index) => {
      const item = document.createElement("li");
      const isExecuted = executed.has(name);
      item.className = "tool-card";
      item.dataset.executed = String(isExecuted);
      item.dataset.meta = String(index === tools.length - 1);
      const title = document.createElement("span");
      title.className = "tool-name";
      title.textContent = label;
      const state = document.createElement("span");
      state.className = "tool-status";
      state.dataset.executed = String(isExecuted);
      state.textContent = isExecuted ? "本次已执行" : "本次未执行";
      item.append(title, state);
      return item;
    }),
  );
}

function renderTimeline() {
  const visibleEvents = replay.events.slice(0, replay.cursor);
  elements.eventTimeline.replaceChildren(
    ...visibleEvents.map((event) => {
      const item = document.createElement("li");
      item.className = "timeline-item";
      const sequence = document.createElement("span");
      sequence.className = "sequence";
      sequence.textContent = String(event.sequence);
      const copy = document.createElement("div");
      const title = document.createElement("div");
      title.className = "event-title";
      title.textContent = EVENT_LABELS[event.type] || "安全事件";
      const meta = document.createElement("p");
      meta.className = "event-meta";
      const details = [];
      if (event.toolName) details.push(`工具：${event.toolName}`);
      if (event.childId) details.push(`分支：${event.childId}`);
      if (event.status) details.push(`状态：${event.status}`);
      meta.textContent = details.join(" · ") || "公开安全事件";
      copy.append(title, meta);
      item.append(sequence, copy);
      return item;
    }),
  );
}

function updateReplayControls() {
  const isComplete = replay.cursor === replay.events.length;
  elements.play.disabled = replay.status === "running" || replay.events.length === 0;
  elements.pause.disabled = replay.status !== "running";
  elements.reset.disabled = replay.events.length === 0;
  if (replay.status === "running") {
    elements.play.textContent = "正在回放";
    setStatus(elements.replayStatus, `回放中 · ${replay.cursor}/${replay.events.length}`, "active");
  } else if (replay.status === "paused") {
    elements.play.textContent = "继续回放";
    setStatus(elements.replayStatus, `已暂停 · ${replay.cursor}/${replay.events.length}`);
  } else if (isComplete && replay.events.length > 0) {
    elements.play.textContent = "从头回放";
    setStatus(elements.replayStatus, `已完成 · ${replay.events.length}/${replay.events.length}`, "active");
  } else {
    elements.play.textContent = "开始回放";
    setStatus(elements.replayStatus, `等待开始 · ${replay.cursor}/${replay.events.length}`);
  }
}

function stopTimer() {
  if (replay.timer !== null) {
    window.clearTimeout(replay.timer);
    replay.timer = null;
  }
}

function advanceReplay() {
  if (replay.status !== "running") return;
  replay.cursor += 1;
  renderTimeline();
  if (replay.cursor >= replay.events.length) {
    replay.status = "completed";
    replay.timer = null;
    updateReplayControls();
    return;
  }
  updateReplayControls();
  replay.timer = window.setTimeout(advanceReplay, STEP_DELAY_MS);
}

function startOrContinueReplay() {
  stopTimer();
  if (replay.status === "completed") replay.cursor = 0;
  replay.status = "running";
  advanceReplay();
}

function pauseReplay() {
  if (replay.status !== "running") return;
  stopTimer();
  replay.status = "paused";
  updateReplayControls();
}

function resetReplay() {
  stopTimer();
  replay.cursor = 0;
  replay.status = "idle";
  renderTimeline();
  updateReplayControls();
}

function renderBenchmark(summary) {
  elements.metricCards.replaceChildren(
    ...METRICS.map(([key, label]) => {
      const metric = summary.metrics[key];
      const card = document.createElement("article");
      card.className = "metric-card";
      const metricLabel = document.createElement("span");
      metricLabel.className = "metric-label";
      metricLabel.textContent = label;
      const value = document.createElement("strong");
      value.className = "metric-value";
      value.textContent = Number(metric.value).toFixed(3);
      const detail = document.createElement("span");
      detail.className = "metric-detail";
      detail.textContent = `分母 ${metric.denominator} · 排除 ${metric.excluded}`;
      card.append(metricLabel, value, detail);
      return card;
    }),
  );
  const provenance = [
    ["Benchmark", summary.benchmark_id],
    ["Scorer", summary.scorer_version],
    ["样本", `${summary.counts.queries} queries · ${summary.counts.products} products`],
    ["标签", `Exact ${summary.label_distribution.Exact} · Substitute ${summary.label_distribution.Substitute}`],
    ["Source revision", summary.source.revision],
    ["Manifest", summary.artifact_manifest_sha256],
  ];
  elements.provenance.replaceChildren(
    ...provenance.map(([term, value]) => {
      const group = document.createElement("div");
      const title = document.createElement("dt");
      title.textContent = term;
      const detail = document.createElement("dd");
      detail.textContent = value;
      group.append(title, detail);
      return group;
    }),
  );
  setStatus(elements.benchmarkStatus, "已验证的离线摘要", "active");
}

async function loadShowcase() {
  try {
    const [replayResponse, summaryResponse] = await Promise.all([
      fetch("./assets/m1d-replay.v1.json"),
      fetch("./assets/m1e-summary.v1.json"),
    ]);
    if (!replayResponse.ok || !summaryResponse.ok) throw new Error("asset load failed");
    const [replayAsset, summaryAsset] = await Promise.all([
      replayResponse.json(),
      summaryResponse.json(),
    ]);
    replay.events = replayAsset.events;
    renderTools(replay.events);
    renderTimeline();
    updateReplayControls();
    renderBenchmark(summaryAsset);
  } catch (_error) {
    setStatus(elements.replayStatus, "展示资产加载失败", "error");
    setStatus(elements.benchmarkStatus, "展示资产加载失败", "error");
  }
}

elements.play.addEventListener("click", startOrContinueReplay);
elements.pause.addEventListener("click", pauseReplay);
elements.reset.addEventListener("click", resetReplay);
void loadShowcase();
