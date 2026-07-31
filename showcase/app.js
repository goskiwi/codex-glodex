"use strict";

/* ===================================================================
   Glodex M1f Showcase — Application Logic
   Pure vanilla JS, zero dependencies, offline-only
   =================================================================== */

/* --- Fixed tool surface (9 business tools + 1 meta tool) --- */
const TOOLS = [
  { name: "planner", desc: "拆解需求，规划执行路径", meta: false },
  { name: "chat_fallback", desc: "无法调用工具时的对话兜底", meta: false },
  { name: "web_search", desc: "基于离线快照的网络检索", meta: false },
  { name: "category_insight", desc: "品类趋势与竞争分析", meta: false },
  { name: "item_search", desc: "在候选池中检索商品", meta: false },
  { name: "item_picker", desc: "从候选中筛选最佳商品", meta: false },
  { name: "price_compare", desc: "多来源价格信息对比", meta: false },
  { name: "shipping_calc", desc: "配送费用与时效估算", meta: false },
  { name: "shopping_summary", desc: "汇总决策结果与建议", meta: false },
  { name: "dispatch_tool", desc: "元工具 · 创建子 Agent 执行子任务", meta: true },
];

/* --- Event type metadata --- */
const EVENT_META = {
  AGENT_STARTED:  { label: "Agent 启动",       tone: "agent"    },
  MODEL_STARTED:  { label: "模型推理开始",     tone: "model"    },
  MODEL_FINISHED: { label: "模型推理完成",     tone: "model"    },
  TOOL_STARTED:   { label: "工具调用开始",     tone: "tool"     },
  TOOL_FINISHED:  { label: "工具调用完成",     tone: "tool"     },
  FORK_STARTED:   { label: "子 Agent 创建",    tone: "fork"     },
  FORK_FINISHED:  { label: "子 Agent 完成",    tone: "fork"     },
  AGENT_RESULT:   { label: "Agent 结果",      tone: "terminal" },
  AGENT_ERROR:    { label: "Agent 安全终止",   tone: "error"    },
};

/* --- Benchmark metric display order --- */
const METRICS = [
  { key: "ndcg_at_10", label: "nDCG@10"  },
  { key: "mrr_at_10",  label: "MRR@10"   },
  { key: "exact_at_10", label: "Exact@10" },
];

/* --- Replay config --- */
const STEP_DELAY_MS = 600;
const SKELETON_COUNT = 4;

/* --- Data source paths (relative, offline) --- */
const REPLAY_URL  = "./assets/m1d-replay.v1.json";
const SUMMARY_URL = "./assets/m1e-summary.v1.json";

/* --- DOM references --- */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const el = {
  timeline:       $("#event-timeline"),
  tools:          $("#tool-inventory"),
  metrics:        $("#metric-cards"),
  provenance:     $("#benchmark-provenance"),
  replayStatus:   $("#replay-status"),
  benchmarkStatus:$("#benchmark-status"),
  play:           $("#play-button"),
  pause:          $("#pause-button"),
  reset:          $("#reset-button"),
  progressFill:   $("#progress-fill"),
  progressTrack:  $(".progress-track"),
  playLabel:      $("#play-button .btn-text"),
};

/* --- Replay state machine --- */
const state = {
  status: "idle",
  events: [],
  cursor: 0,
  renderedCount: 0,
  speed: 1,
  timer: null,
};

/* ===================================================================
   Utilities
   =================================================================== */

function formatTimestamp(ms) {
  if (ms === undefined || ms === null) return "";
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const r = Math.floor(s % 60);
  return `${m}:${String(r).padStart(2, "0")}`;
}

function setPill(element, text, tone) {
  element.textContent = text;
  element.dataset.tone = tone || "";
}

/* ===================================================================
   Progress Bar
   =================================================================== */

function updateProgress() {
  const total = state.events.length;
  const percent = total === 0 ? 0 : Math.round((state.cursor / total) * 100);
  el.progressFill.style.width = `${percent}%`;
  el.progressTrack.setAttribute("aria-valuenow", String(percent));
  const label = total === 0 ? "等待数据" : `${state.cursor}/${total}`;
  el.progressTrack.setAttribute("aria-valuetext", `${percent}% — ${label}`);
}

/* ===================================================================
   Tool Inventory
   =================================================================== */

function renderTools(events) {
  const executed = new Set();
  events.forEach((e) => {
    if (e.toolName) executed.add(e.toolName);
  });

  const frag = document.createDocumentFragment();

  TOOLS.forEach((tool) => {
    const isExecuted = executed.has(tool.name);
    const li = document.createElement("li");
    li.className = "tool-card";
    li.dataset.executed = String(isExecuted);
    li.dataset.meta = String(tool.meta);

    /* Tool head: name + status dot */
    const head = document.createElement("div");
    head.className = "tool-head";

    const name = document.createElement("span");
    name.className = "tool-name";
    name.textContent = tool.name;

    const dot = document.createElement("span");
    dot.className = "tool-dot";

    head.append(name, dot);

    /* Description */
    const desc = document.createElement("p");
    desc.className = "tool-desc";
    desc.textContent = tool.desc;

    /* Execution state label */
    const stateLabel = document.createElement("span");
    stateLabel.className = "tool-state";
    stateLabel.textContent = isExecuted ? "本次已执行" : "本次未执行";

    li.append(head, desc, stateLabel);
    frag.append(li);
  });

  el.tools.replaceChildren(frag);
}

/* ===================================================================
   Timeline — Incremental Rendering
   =================================================================== */

function createEventItem(event) {
  const meta = EVENT_META[event.type] || { label: "安全事件", tone: "agent" };
  const li = document.createElement("li");
  li.className = "event-item";
  if (event.scope === "child") li.classList.add("event-child");
  li.dataset.tone = meta.tone;
  li.dataset.scope = event.scope || "root";

  /* Sequence badge */
  const seq = document.createElement("span");
  seq.className = "event-seq";
  seq.textContent = String(event.sequence);

  /* Event body */
  const body = document.createElement("div");
  body.className = "event-body";

  /* Header row: label + tool/branch + timestamp */
  const head = document.createElement("div");
  head.className = "event-head";

  const label = document.createElement("span");
  label.className = "event-label";
  label.textContent = meta.label;
  head.append(label);

  if (event.toolName) {
    const tool = document.createElement("span");
    tool.className = "event-tool";
    tool.textContent = event.toolName;
    head.append(tool);
  }

  if (event.childId) {
    const branch = document.createElement("span");
    branch.className = "event-branch";
    branch.textContent = event.childId;
    head.append(branch);
  }

  if (event.timestamp !== undefined) {
    const ts = document.createElement("span");
    ts.className = "event-ts";
    ts.textContent = formatTimestamp(event.timestamp);
    head.append(ts);
  }

  body.append(head);

  /* Meta line: round, safe code */
  const details = [];
  if (event.round) details.push(`轮次 ${event.round}`);
  if (event.safeCode) details.push(`安全码 ${event.safeCode}`);

  if (details.length > 0) {
    const metaP = document.createElement("p");
    metaP.className = "event-meta";
    metaP.textContent = details.join(" · ");
    body.append(metaP);
  }

  li.append(seq, body);
  return li;
}

function renderNewEvents() {
  /* Reset case: cursor went backward */
  if (state.cursor < state.renderedCount) {
    el.timeline.replaceChildren();
    state.renderedCount = 0;
  }

  if (state.cursor === state.renderedCount) return;

  /* Incremental append — only new events */
  const frag = document.createDocumentFragment();
  for (let i = state.renderedCount; i < state.cursor; i++) {
    frag.append(createEventItem(state.events[i]));
  }
  el.timeline.append(frag);
  state.renderedCount = state.cursor;

  /* Auto-scroll to latest item during playback */
  if (state.status === "running" && state.cursor > 0) {
    const last = el.timeline.lastElementChild;
    if (last) {
      last.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }
}

/* ===================================================================
   Replay Controls
   =================================================================== */

function updateControls() {
  const isComplete =
    state.cursor >= state.events.length && state.events.length > 0;

  el.play.disabled = state.status === "running" || state.events.length === 0;
  el.pause.disabled = state.status !== "running";
  el.reset.disabled = state.events.length === 0;

  if (state.status === "running") {
    el.playLabel.textContent = "回放中";
    setPill(el.replayStatus, `${state.cursor}/${state.events.length} 回放中`, "active");
  } else if (state.status === "paused") {
    el.playLabel.textContent = "继续回放";
    setPill(el.replayStatus, `已暂停 · ${state.cursor}/${state.events.length}`, "");
  } else if (isComplete) {
    el.playLabel.textContent = "重新播放";
    setPill(el.replayStatus, "回放完成", "active");
  } else {
    el.playLabel.textContent = "开始回放";
    setPill(
      el.replayStatus,
      state.events.length > 0
        ? `就绪 · ${state.events.length} 事件`
        : "等待加载",
      ""
    );
  }

  updateProgress();
}

function clearTimer() {
  if (state.timer !== null) {
    clearTimeout(state.timer);
    state.timer = null;
  }
}

function advance() {
  if (state.status !== "running") return;

  state.cursor += 1;
  renderNewEvents();

  if (state.cursor >= state.events.length) {
    state.status = "completed";
    state.timer = null;
    updateControls();
    return;
  }

  updateControls();
  const delay = STEP_DELAY_MS / state.speed;
  state.timer = setTimeout(advance, delay);
}

function startReplay() {
  clearTimer();

  /* Restart from beginning if already completed */
  if (state.status === "completed") {
    state.cursor = 0;
    state.renderedCount = 0;
    el.timeline.replaceChildren();
  }

  state.status = "running";
  advance();
}

function pauseReplay() {
  if (state.status !== "running") return;
  clearTimer();
  state.status = "paused";
  updateControls();
}

function resetReplay() {
  clearTimer();
  state.cursor = 0;
  state.renderedCount = 0;
  state.status = "idle";
  el.timeline.replaceChildren();
  updateControls();
}

/* ===================================================================
   Speed Control
   =================================================================== */

function setSpeed(speed) {
  state.speed = speed;
  $$(".speed-btn").forEach((btn) => {
    const active = Number(btn.dataset.speed) === speed;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  });
}

/* ===================================================================
   Benchmark Metrics & Provenance
   =================================================================== */

function renderBenchmark(summary) {
  /* Metric cards */
  const frag = document.createDocumentFragment();

  METRICS.forEach((m) => {
    const metric = summary.metrics[m.key];
    const card = document.createElement("article");
    card.className = "metric-card";

    const label = document.createElement("span");
    label.className = "metric-label";
    label.textContent = m.label;

    const value = document.createElement("strong");
    value.className = "metric-value";
    value.textContent = Number(metric.value).toFixed(4);

    const detail = document.createElement("span");
    detail.className = "metric-detail";
    detail.textContent = `分母 ${metric.denominator} · 排除 ${metric.excluded}`;

    card.append(label, value, detail);
    frag.append(card);
  });

  el.metrics.replaceChildren(frag);

  /* Provenance */
  const items = [
    ["Benchmark", summary.benchmark_id],
    ["Scorer", summary.scorer_version],
    ["样本量", `${summary.counts.queries} queries / ${summary.counts.products} products / ${summary.counts.judgements} judgements`],
    ["标签分布", `Exact ${summary.label_distribution.Exact} / Substitute ${summary.label_distribution.Substitute} / Complement ${summary.label_distribution.Complement} / Irrelevant ${summary.label_distribution.Irrelevant}`],
    ["Revision", summary.source.revision],
    ["Manifest", summary.artifact_manifest_sha256],
  ];

  const provFrag = document.createDocumentFragment();

  items.forEach(([term, val]) => {
    const div = document.createElement("div");
    div.className = "prov-item";

    const dt = document.createElement("dt");
    dt.className = "prov-term";
    dt.textContent = term;

    const dd = document.createElement("dd");
    dd.className = "prov-value";
    dd.textContent = val;

    div.append(dt, dd);
    provFrag.append(div);
  });

  el.provenance.replaceChildren(provFrag);
  setPill(el.benchmarkStatus, "已验证", "active");
}

/* ===================================================================
   Skeleton Loading
   =================================================================== */

function showSkeletons() {
  const frag = document.createDocumentFragment();

  for (let i = 0; i < SKELETON_COUNT; i++) {
    const li = document.createElement("li");
    li.className = "event-item skeleton-item";

    const seq = document.createElement("span");
    seq.className = "skeleton-seq";

    const body = document.createElement("div");
    body.className = "skeleton-body";

    const line1 = document.createElement("div");
    line1.className = "skeleton-line skeleton-line-wide";

    const line2 = document.createElement("div");
    line2.className = "skeleton-line skeleton-line-narrow";

    body.append(line1, line2);
    li.append(seq, body);
    frag.append(li);
  }

  el.timeline.replaceChildren(frag);
}

/* ===================================================================
   Data Loading
   =================================================================== */

async function init() {
  showSkeletons();

  try {
    const [replayRes, summaryRes] = await Promise.all([
      fetch(REPLAY_URL),
      fetch(SUMMARY_URL),
    ]);

    if (!replayRes.ok || !summaryRes.ok) {
      throw new Error("asset load failed");
    }

    const [replayData, summaryData] = await Promise.all([
      replayRes.json(),
      summaryRes.json(),
    ]);

    state.events = replayData.events;
    el.timeline.replaceChildren();
    state.renderedCount = 0;

    renderTools(state.events);
    renderNewEvents();
    updateControls();
    renderBenchmark(summaryData);
  } catch (_err) {
    el.timeline.replaceChildren();
    setPill(el.replayStatus, "加载失败", "error");
    setPill(el.benchmarkStatus, "加载失败", "error");
  }
}

/* ===================================================================
   Event Listeners
   =================================================================== */

el.play.addEventListener("click", startReplay);
el.pause.addEventListener("click", pauseReplay);
el.reset.addEventListener("click", resetReplay);

$$(".speed-btn").forEach((btn) => {
  btn.addEventListener("click", () => setSpeed(Number(btn.dataset.speed)));
});

/* Keyboard shortcuts: Space = play/pause, R = reset */
document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "BUTTON" || e.target.tagName === "INPUT") return;

  if (e.code === "Space") {
    e.preventDefault();
    if (state.status === "running") {
      pauseReplay();
    } else if (!el.play.disabled) {
      startReplay();
    }
  } else if (e.code === "KeyR") {
    e.preventDefault();
    if (!el.reset.disabled) {
      resetReplay();
    }
  }
});

/* --- Boot --- */
init();
