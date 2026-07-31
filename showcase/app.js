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
const BASE_STEP_DELAY_MS = 650;
const SKELETON_COUNT = 4;
const SVG_NS = ["http:", "", "www.w3.org", "2000", "svg"].join("/");

const $ = (selector) => document.querySelector(selector);

const elements = {
  benchmarkStatus: $("#benchmark-status"),
  benchmarkProvenance: $("#benchmark-provenance"),
  eventTimeline: $("#event-timeline"),
  metricCards: $("#metric-cards"),
  pause: $("#pause-button"),
  play: $("#play-button"),
  progressBar: $("#replay-progress-bar"),
  progressContainer: $(".replay-progress"),
  replayStatus: $("#replay-status"),
  reset: $("#reset-button"),
  toolInventory: $("#tool-inventory"),
};

const replay = {
  cursor: 0,
  events: [],
  renderedCount: 0,
  status: "idle",
  timer: null,
  speed: 1,
};

/* ---------- Utilities ---------- */

function formatTimestamp(ms) {
  if (ms < 1000) return `${ms}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return `${mins}:${String(secs).padStart(2, "0")}`;
}

function svgIcon(id) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", "icon");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", `#${id}`);
  svg.append(use);
  return svg;
}

function setStatus(element, text, tone = "") {
  element.textContent = text;
  element.dataset.tone = tone;
}

/* ---------- Progress bar ---------- */

function updateProgress() {
  const total = replay.events.length;
  const percent = total === 0 ? 0 : Math.round((replay.cursor / total) * 100);
  elements.progressBar.style.width = `${percent}%`;
  elements.progressContainer.setAttribute("aria-valuenow", String(percent));
  const text = total === 0 ? "等待数据" : `${replay.cursor}/${total} 事件`;
  elements.progressContainer.setAttribute("aria-valuetext", `${percent}% — ${text}`);
}

/* ---------- Tool inventory ---------- */

function renderTools(events) {
  const executed = new Set(events.map((event) => event.toolName).filter(Boolean));
  const tools = [...BUSINESS_TOOLS, META_TOOL];
  const fragment = document.createDocumentFragment();
  tools.forEach(([name, label], index) => {
    const item = document.createElement("li");
    const isExecuted = executed.has(name);
    const isMeta = index === tools.length - 1;
    item.className = "tool-card";
    item.dataset.executed = String(isExecuted);
    item.dataset.meta = String(isMeta);

    const nameWrap = document.createElement("span");
    nameWrap.className = "tool-name";

    const badge = document.createElement("span");
    badge.className = "tool-badge";
    badge.append(svgIcon(isMeta ? "i-branch" : "i-zap"));

    const nameText = document.createElement("span");
    nameText.textContent = label;

    nameWrap.append(badge, nameText);

    const state = document.createElement("span");
    state.className = "tool-status";
    state.dataset.executed = String(isExecuted);
    state.textContent = isExecuted ? "本次已执行" : "本次未执行";

    item.append(nameWrap, state);
    fragment.append(item);
  });
  elements.toolInventory.replaceChildren(fragment);
}

/* ---------- Timeline (incremental render) ---------- */

function createTimelineItem(event) {
  const item = document.createElement("li");
  item.className = "timeline-item";
  item.dataset.type = event.type;
  if (event.scope === "child") item.dataset.scope = "child";

  const sequence = document.createElement("span");
  sequence.className = "sequence";
  sequence.textContent = String(event.sequence);

  const copy = document.createElement("div");

  const header = document.createElement("div");
  header.className = "event-header";

  const title = document.createElement("span");
  title.className = "event-title";
  title.textContent = EVENT_LABELS[event.type] || "安全事件";
  header.append(title);

  if (event.timestamp !== undefined) {
    const time = document.createElement("span");
    time.className = "event-time";
    time.textContent = formatTimestamp(event.timestamp);
    header.append(time);
  }

  if (event.childId) {
    const branch = document.createElement("span");
    branch.className = "event-time";
    branch.style.borderColor = "var(--warning)";
    branch.style.color = "var(--warning)";
    branch.textContent = `分支 ${event.childId}`;
    header.append(branch);
  }

  const meta = document.createElement("p");
  meta.className = "event-meta";
  const details = [];
  if (event.toolName) details.push(`工具：${event.toolName}`);
  if (event.round) details.push(`轮次：${event.round}`);
  if (event.status) details.push(`状态：${event.status}`);
  if (event.safeCode) details.push(`安全码：${event.safeCode}`);
  meta.textContent = details.join(" · ") || "公开安全事件";

  copy.append(header, meta);
  item.append(sequence, copy);
  return item;
}

function renderTimeline() {
  if (replay.cursor < replay.renderedCount) {
    // Reset or rewind — clear all
    elements.eventTimeline.replaceChildren();
    replay.renderedCount = 0;
  }

  if (replay.cursor === replay.renderedCount) return;

  const fragment = document.createDocumentFragment();
  for (let i = replay.renderedCount; i < replay.cursor; i++) {
    fragment.append(createTimelineItem(replay.events[i]));
  }
  elements.eventTimeline.append(fragment);
  replay.renderedCount = replay.cursor;

  // Auto-scroll to latest item during playback
  if (replay.status === "running" && replay.cursor > 0) {
    const lastItem = elements.eventTimeline.lastElementChild;
    if (lastItem) {
      lastItem.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }
}

/* ---------- Replay controls ---------- */

function updateReplayControls() {
  const isComplete = replay.cursor === replay.events.length && replay.events.length > 0;
  elements.play.disabled = replay.status === "running" || replay.events.length === 0;
  elements.pause.disabled = replay.status !== "running";
  elements.reset.disabled = replay.events.length === 0;

  const playLabel = elements.play.querySelector("span");

  if (replay.status === "running") {
    playLabel.textContent = "正在回放";
    setStatus(elements.replayStatus, `回放中 · ${replay.cursor}/${replay.events.length}`, "active");
  } else if (replay.status === "paused") {
    playLabel.textContent = "继续回放";
    setStatus(elements.replayStatus, `已暂停 · ${replay.cursor}/${replay.events.length}`);
  } else if (isComplete) {
    playLabel.textContent = "从头回放";
    setStatus(elements.replayStatus, `已完成 · ${replay.events.length}/${replay.events.length}`, "active");
  } else {
    playLabel.textContent = "开始回放";
    setStatus(elements.replayStatus, `等待开始 · ${replay.cursor}/${replay.events.length}`);
  }

  updateProgress();
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
  const delay = BASE_STEP_DELAY_MS / replay.speed;
  replay.timer = window.setTimeout(advanceReplay, delay);
}

function startOrContinueReplay() {
  stopTimer();
  if (replay.status === "completed") {
    replay.cursor = 0;
    replay.renderedCount = 0;
    elements.eventTimeline.replaceChildren();
  }
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
  replay.renderedCount = 0;
  replay.status = "idle";
  elements.eventTimeline.replaceChildren();
  updateReplayControls();
}

/* ---------- Speed control ---------- */

function setSpeed(speed) {
  replay.speed = speed;
  document.querySelectorAll(".speed-btn").forEach((btn) => {
    const isActive = Number(btn.dataset.speed) === speed;
    btn.classList.toggle("active", isActive);
    btn.setAttribute("aria-pressed", String(isActive));
  });
}

/* ---------- Benchmark ---------- */

function renderBenchmark(summary) {
  const metricFragment = document.createDocumentFragment();
  METRICS.forEach(([key, label]) => {
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
    metricFragment.append(card);
  });
  elements.metricCards.replaceChildren(metricFragment);

  const provenance = [
    ["Benchmark", summary.benchmark_id],
    ["Scorer", summary.scorer_version],
    ["样本", `${summary.counts.queries} queries · ${summary.counts.products} products`],
    ["标签", `Exact ${summary.label_distribution.Exact} · Substitute ${summary.label_distribution.Substitute}`],
    ["Source revision", summary.source.revision],
    ["Manifest", summary.artifact_manifest_sha256],
  ];
  const provFragment = document.createDocumentFragment();
  provenance.forEach(([term, value]) => {
    const group = document.createElement("div");
    const title = document.createElement("dt");
    title.textContent = term;
    const detail = document.createElement("dd");
    detail.textContent = value;
    group.append(title, detail);
    provFragment.append(group);
  });
  elements.benchmarkProvenance.replaceChildren(provFragment);
  setStatus(elements.benchmarkStatus, "已验证的离线摘要", "active");
}

/* ---------- Skeleton loading ---------- */

function showSkeletons() {
  const fragment = document.createDocumentFragment();
  for (let i = 0; i < SKELETON_COUNT; i++) {
    const item = document.createElement("li");
    item.className = "skeleton-item";
    const circle = document.createElement("div");
    circle.className = "skeleton-circle";
    const lines = document.createElement("div");
    const line1 = document.createElement("div");
    line1.className = "skeleton-line mid";
    const line2 = document.createElement("div");
    line2.className = "skeleton-line short";
    lines.append(line1, line2);
    item.append(circle, lines);
    fragment.append(item);
  }
  elements.eventTimeline.replaceChildren(fragment);
}

/* ---------- Data loading ---------- */

async function loadShowcase() {
  showSkeletons();
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
    elements.eventTimeline.replaceChildren();
    replay.renderedCount = 0;
    renderTools(replay.events);
    renderTimeline();
    updateReplayControls();
    renderBenchmark(summaryAsset);
  } catch (_error) {
    elements.eventTimeline.replaceChildren();
    setStatus(elements.replayStatus, "展示资产加载失败", "error");
    setStatus(elements.benchmarkStatus, "展示资产加载失败", "error");
  }
}

/* ---------- Event listeners ---------- */

elements.play.addEventListener("click", startOrContinueReplay);
elements.pause.addEventListener("click", pauseReplay);
elements.reset.addEventListener("click", resetReplay);

document.querySelectorAll(".speed-btn").forEach((btn) => {
  btn.addEventListener("click", () => setSpeed(Number(btn.dataset.speed)));
});

// Keyboard shortcuts (Space = play/pause, R = reset)
document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "BUTTON" || e.target.tagName === "INPUT") return;
  if (e.code === "Space") {
    e.preventDefault();
    if (replay.status === "running") pauseReplay();
    else if (!elements.play.disabled) startOrContinueReplay();
  } else if (e.code === "KeyR") {
    e.preventDefault();
    if (!elements.reset.disabled) resetReplay();
  }
});

void loadShowcase();
