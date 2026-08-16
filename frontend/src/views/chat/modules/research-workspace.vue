<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import type {
  ShoppingAgentFork,
  ShoppingAgentLifecycleEvent,
  ShoppingAgentResponse,
  ShoppingAgentResult,
  ShoppingAgentStage,
  ShoppingAgentStageState,
  ShoppingAgentTraceStep,
  ShoppingPlatform
} from '@/service/agent';
import { cancelShoppingAgentRun, replayShoppingAgentRun, runShoppingAgent } from '@/service/agent';
import type { ConversationTurn } from '@/service/memory';
import { useCartStore } from '@/store/modules/cart';
import { useResearchHistoryStore } from '@/store/modules/research-history';

defineOptions({
  name: 'ResearchWorkspace'
});

type ResearchState = 'idle' | 'researching' | 'complete' | 'empty' | 'error';

interface Candidate {
  id: string;
  title: string;
  category: string;
  source: string;
  price: string;
  description: string;
  metadata: string[];
  requirements: Array<{
    label: string;
    importance: ShoppingAgentResult['needCoverage'][number]['importance'];
    status: ShoppingAgentResult['needCoverage'][number]['status'];
    statusLabel: string;
    detail: string;
    knownFacts: string[];
    missingEvidence?: string;
    conclusion: string;
    evidence: ShoppingAgentResult['needCoverage'][number]['evidence'];
  }>;
  evidence: ShoppingAgentResult['evidence'];
}

interface ResearchProfile {
  title: string;
  summary: string;
  candidates: Candidate[];
}

type EvidenceFact = ShoppingAgentResult['evidence'][number];

interface EvidenceSourceGroup {
  key: string;
  sourceLabel: string;
  sourceUrl: string;
  hostname: string;
  fieldSummary: string;
  factCount: number;
}

const platformLabels: Record<ShoppingPlatform, string> = {
  amazon: 'Amazon',
  shopee: 'Shopee',
  aliexpress: 'AliExpress',
  ebay: 'eBay',
  alibaba: 'Alibaba',
  walmart: 'Walmart',
  shein: 'SHEIN'
};

const tracePhaseLabels = {
  THINK: 'Think',
  ACT: 'Act',
  OBSERVE: 'Observe',
  REFLECT: 'Reflect'
} as const;

function isLiveModelStream(step: ShoppingAgentTraceStep) {
  return step.state === 'RUNNING' && step.title.startsWith('模型正在流式');
}

function getTopic(input: string) {
  const topic = input.split(/[，,。；;！!]/)[0]?.trim();
  return topic ? topic.slice(0, 18) : '商品研究';
}

const needStatusLabels: Record<ShoppingAgentResult['needCoverage'][number]['status'], string> = {
  VERIFIED: '有证据支持',
  UNVERIFIED: '缺少证据',
  MISSING: '无相关数据',
  INSUFFICIENT: '证据不足',
  CONFLICTED: '证据冲突'
};

function needStatusLabel(item: ShoppingAgentResult['needCoverage'][number]) {
  if (item.status === 'VERIFIED' && item.importance === 'required') return '满足';
  return needStatusLabels[item.status];
}

function coverageMetadata(item: ShoppingAgentResult['needCoverage'][number]) {
  const summary = `${item.label}：${needStatusLabel(item)}`;
  const keyFact = item.importance === 'preferred' ? item.knownFacts[0] : undefined;
  return keyFact ? `${summary}（${keyFact}）` : summary;
}

function metadataForResult(result: ShoppingAgentResult): string[] {
  return [
    `类目：${result.categoryLabel}`,
    `到手价：${result.selectedOffer.landedCost} ${result.selectedOffer.currency}`,
    ...result.needCoverage.map(coverageMetadata)
  ];
}

function sourceForResult(result: ShoppingAgentResult) {
  const platform = result.productId.split('.', 1)[0] as keyof typeof platformLabels;
  return platformLabels[platform] || '商品来源';
}

function toCandidate(result: ShoppingAgentResult): Candidate {
  const metadata = metadataForResult(result);
  const source = result.evidence[0]?.sourceLabel || result.selectedOffer.sourceLabel || sourceForResult(result);

  return {
    id: result.productId,
    title: result.title,
    category: result.categoryLabel,
    source,
    price: `${result.selectedOffer.landedCost} ${result.selectedOffer.currency}`,
    description: metadata.join(' · '),
    metadata,
    requirements: result.needCoverage.map(item => ({
      label: item.label,
      importance: item.importance,
      status: item.status,
      statusLabel: needStatusLabel(item),
      detail: item.detail,
      knownFacts: item.knownFacts,
      missingEvidence: item.missingEvidence,
      conclusion: item.conclusion,
      evidence: item.evidence
    })),
    evidence: result.evidence
  };
}

function sourceHostname(sourceUrl: string) {
  try {
    return new URL(sourceUrl).hostname.replace(/^www\./, '');
  } catch {
    return '厂商官网';
  }
}

function summarizeFieldLabels(labels: string[]) {
  const readableLabels = labels.filter(label => !/^[a-z][a-z0-9_]*$/.test(label));
  if (!readableLabels.length) return '用于核对商品基础信息与官方规格';
  const visibleLabels = readableLabels.slice(0, 5);
  const remainder = readableLabels.length - visibleLabels.length;
  return `核验：${visibleLabels.join('、')}${remainder > 0 ? `等 ${readableLabels.length} 项` : ''}`;
}

function groupEvidenceBySource(facts: EvidenceFact[], relevantFacts: EvidenceFact[] = facts): EvidenceSourceGroup[] {
  const relevantLabelsBySource = new Map<string, Set<string>>();
  for (const fact of relevantFacts) {
    const key = `${fact.sourceLabel}\u0000${fact.sourceUrl}`;
    const labels = relevantLabelsBySource.get(key) ?? new Set<string>();
    labels.add(fact.fieldLabel);
    relevantLabelsBySource.set(key, labels);
  }

  const groups = new Map<string, Omit<EvidenceSourceGroup, 'fieldSummary'> & { factIds: Set<string> }>();
  for (const fact of facts) {
    const key = `${fact.sourceLabel}\u0000${fact.sourceUrl}`;
    const group = groups.get(key) ?? {
      key,
      sourceLabel: fact.sourceLabel,
      sourceUrl: fact.sourceUrl,
      hostname: sourceHostname(fact.sourceUrl),
      factCount: 0,
      factIds: new Set<string>()
    };
    group.factIds.add(fact.evidenceId);
    group.factCount = group.factIds.size;
    groups.set(key, group);
  }

  return [...groups.values()].map(({ factIds: _factIds, ...group }) => ({
    ...group,
    fieldSummary: summarizeFieldLabels([...(relevantLabelsBySource.get(group.key) ?? [])])
  }));
}

const researchState = ref<ResearchState>('idle');
const brief = ref('');
const composerBrief = ref('');
const composerInput = ref<HTMLTextAreaElement | null>(null);
const showProcess = ref(false);
const selectedCandidate = ref<Candidate | null>(null);
const agentStages = ref<ShoppingAgentStage[]>([]);
const agentForks = ref<ShoppingAgentFork[]>([]);
const agentTrace = ref<ShoppingAgentTraceStep[]>([]);
const agentResponse = ref<ShoppingAgentResponse | null>(null);
const runErrorCode = ref('');
const isHistoryReplay = ref(false);
const assistantMessage = ref('');
const activeAssistantMessageId = ref<string | null>(null);
const errorMessage = ref('');
const cartStore = useCartStore();
const researchHistoryStore = useResearchHistoryStore();
const selectedCandidateIds = ref<string[]>([]);
let activeRequest: AbortController | null = null;
const activeDurableRunId = ref<string | null>(null);
const activeThreadId = ref<string | null>(null);
const conversationTurns = ref<ConversationTurn[]>([]);

const currentProfile = computed<ResearchProfile>(() => {
  const response = agentResponse.value;

  if (!response) {
    return {
      title: brief.value ? getTopic(brief.value) : '开始一项研究',
      summary: '输入商品用途、品牌、类目或限制后，查询当前商品库。',
      candidates: []
    };
  }

  return {
    title: getTopic(response.query),
    summary: response.summary,
    candidates: response.results.map(toCandidate)
  };
});
const processSteps = computed(() => agentTrace.value);
const activeProcessStep = computed(() => [...processSteps.value].reverse().find(step => step.state === 'RUNNING'));
const correctedActionCount = computed(
  () => agentTrace.value.filter(step => step.safeCode === 'INVALID_ACTION').length
);
const completedProcessCount = computed(
  () => processSteps.value.filter(step => step.state === 'FINISHED').length
);
const assistantAnswer = computed(() => assistantMessage.value || agentResponse.value?.summary || '');
const conversationTranscriptTurns = computed(() => {
  const currentRunId = agentResponse.value?.runId;
  if (!currentRunId) return conversationTurns.value;
  return conversationTurns.value.filter(turn => turn.terminalRunId !== currentRunId);
});
const selectedEvidenceSources = computed(() => {
  const candidate = selectedCandidate.value;
  if (!candidate) return [];
  const decisionFacts = candidate.requirements.flatMap(requirement => requirement.evidence);
  return groupEvidenceBySource(candidate.evidence, decisionFacts);
});
const selectedCandidateCount = computed(() => selectedCandidateIds.value.length);
const selectedCandidates = computed(() =>
  currentProfile.value.candidates
    .map((candidate, index) => ({ candidate, index }))
    .filter(({ candidate }) => selectedCandidateIds.value.includes(candidate.id))
);
const selectedCandidatesAreFavorited = computed(
  () =>
    selectedCandidates.value.length > 0 &&
    selectedCandidates.value.every(({ candidate }) => cartStore.hasFavorite(candidate.id))
);

const contextTitle = computed(() => {
  if (researchState.value === 'researching') return '正在检索';
  return currentProfile.value.title;
});

const contextStatus = computed(() => {
  if (isHistoryReplay.value) return '历史回放';
  const status: Record<ResearchState, string> = {
    idle: '待开始',
    researching: '检索中',
    complete: '已完成',
    empty: '无结果',
    error: '未完成'
  };

  return status[researchState.value];
});

const isEvidenceVisible = computed({
  get: () => Boolean(selectedCandidate.value),
  set: visible => {
    if (!visible) selectedCandidate.value = null;
  }
});

function resizeComposer() {
  const textarea = composerInput.value;
  if (!textarea) return;

  textarea.style.height = 'auto';
  textarea.style.height = `${Math.min(textarea.scrollHeight, 112)}px`;
}

function focusResearchComposer(includeCurrentBrief = false) {
  if (includeCurrentBrief) composerBrief.value = brief.value;
  window.requestAnimationFrame(() => {
    resizeComposer();
    composerInput.value?.focus();
  });
}

function isAbortError(error: unknown) {
  return error instanceof DOMException && error.name === 'AbortError';
}

function startProcessStep(stageName: string) {
  const existing = [...agentStages.value].reverse().find(stage => stage.name === stageName && stage.state === 'RUNNING');
  if (existing) return;

  agentStages.value = [...agentStages.value, { name: stageName, state: 'RUNNING', platforms: [] }];
}

function finishProcessStep(stageName: string) {
  const stages = [...agentStages.value];
  const index = stages.map(stage => stage.name).lastIndexOf(stageName);
  if (index < 0) {
    agentStages.value = [...stages, { name: stageName, state: 'FINISHED', platforms: [] }];
    return;
  }

  stages[index] = { ...stages[index], state: 'FINISHED' };
  agentStages.value = stages;
}

function attachToolCall(toolCallName: string, toolCallId: string) {
  const stagePrefix = `tool:${toolCallName}`;
  const stages = [...agentStages.value];
  const index = stages
    .map(stage => stage.name)
    .findLastIndex(stageName => stageName === stagePrefix || stageName.startsWith(`${stagePrefix}:`));

  if (index < 0) {
    startProcessStep(stagePrefix);
    attachToolCall(toolCallName, toolCallId);
    return;
  }

  stages[index] = { ...stages[index], toolCallId };
  agentStages.value = stages;
}

function closeToolCall(toolCallId: string) {
  const stages = agentStages.value.map(stage =>
    stage.toolCallId === toolCallId && stage.state === 'RUNNING'
      ? { ...stage, state: 'FINISHED' as ShoppingAgentStageState }
      : stage
  );
  agentStages.value = stages;
}

function applyAgentEvent(event: ShoppingAgentLifecycleEvent) {
  switch (event.type) {
    case 'RUN_STARTED':
      activeDurableRunId.value = event.runId;
      activeThreadId.value = event.threadId;
      refreshConversationTurns(event.threadId).catch(() => undefined);
      break;
    case 'RUN_FINISHED':
      activeDurableRunId.value = null;
      break;
    case 'RUN_ERROR':
      runErrorCode.value = event.code;
      break;
    case 'STEP_STARTED':
      startProcessStep(event.stepName);
      break;
    case 'STEP_FINISHED':
      finishProcessStep(event.stepName);
      break;
    case 'TOOL_CALL_START':
      attachToolCall(event.toolCallName, event.toolCallId);
      break;
    case 'TOOL_CALL_END':
      closeToolCall(event.toolCallId);
      break;
    case 'STATE_SNAPSHOT':
      agentStages.value = event.stages;
      agentForks.value = event.forks;
      agentTrace.value = event.trace;
      break;
    case 'TEXT_MESSAGE_START':
      activeAssistantMessageId.value = event.messageId;
      assistantMessage.value = '';
      break;
    case 'TEXT_MESSAGE_CONTENT':
      if (activeAssistantMessageId.value === event.messageId) {
        assistantMessage.value += event.delta;
      }
      break;
    case 'TEXT_MESSAGE_END':
      if (activeAssistantMessageId.value === event.messageId) activeAssistantMessageId.value = null;
      break;
    default:
      break;
  }
}

async function refreshConversationTurns(threadId: string) {
  try {
    conversationTurns.value = await researchHistoryStore.turns(threadId);
  } catch {
    window.$message?.warning('本轮仍在继续，但对话记录暂时无法刷新。');
  }
}

async function cancelActiveResearch() {
  const controller = activeRequest;
  const runId = activeDurableRunId.value;
  if (runId) {
    try {
      await cancelShoppingAgentRun(runId);
    } catch {
      window.$message?.warning('本地连接已停止，但 Agent 取消请求未能确认。');
    }
  }
  activeRequest = null;
  activeDurableRunId.value = null;
  controller?.abort();
}

async function cancelResearch() {
  await cancelActiveResearch();
  researchState.value = 'idle';
  showProcess.value = false;
  window.$message?.info('已取消本次商品研究。');
}

async function runResearch(input = composerBrief.value) {
  const nextBrief = input.trim();

  if (!nextBrief) {
    window.$message?.warning('请先写下用途、商品或限制。');
    return;
  }

  await cancelActiveResearch();
  const controller = new AbortController();
  activeRequest = controller;
  brief.value = nextBrief;
  composerBrief.value = '';
  selectedCandidateIds.value = [];
  selectedCandidate.value = null;
  agentResponse.value = null;
  isHistoryReplay.value = false;
  runErrorCode.value = '';
  assistantMessage.value = '';
  activeAssistantMessageId.value = null;
  agentStages.value = [];
  agentForks.value = [];
  agentTrace.value = [];
  errorMessage.value = '';
  researchState.value = 'researching';
  showProcess.value = true;
  window.requestAnimationFrame(resizeComposer);

  try {
    const response = await runShoppingAgent(nextBrief, {
      topK: 3,
      threadId: activeThreadId.value ?? undefined,
      signal: controller.signal,
      onEvent: event => {
        if (activeRequest !== controller || controller.signal.aborted) return;
        applyAgentEvent(event);
      }
    });
    if (controller.signal.aborted) return;

    brief.value = response.query;
    activeThreadId.value = response.threadId;
    agentResponse.value = response;
    if (!assistantMessage.value) assistantMessage.value = response.summary;
    researchState.value = response.results.length || response.contentKind === 'CHAT_FALLBACK' ? 'complete' : 'empty';
    await refreshConversationTurns(response.threadId);
  } catch (error) {
    if (isAbortError(error)) return;
    if (activeRequest !== controller) return;

    agentResponse.value = null;
    if (runErrorCode.value === 'SHOPPING_SUMMARY_INVALID') {
      errorMessage.value =
        '商品检索和候选核验已经完成，但最终说明没有通过安全校验。可直接重试；系统不会展示未经校验的模型文本。';
    } else if (error instanceof Error) {
      errorMessage.value = error.message;
    } else {
      errorMessage.value = '商品研究 Agent 未能完成，请稍后重试。';
    }
    researchState.value = 'error';
    if (activeThreadId.value) await refreshConversationTurns(activeThreadId.value);
  } finally {
    if (activeRequest === controller) {
      activeRequest = null;
      activeDurableRunId.value = null;
    }
  }
}

async function loadHistoricalResearch(threadId: string | null) {
  if (!threadId) return;
  try {
    const turns = await researchHistoryStore.turns(threadId);
    const lastUser = [...turns].reverse().find(turn => turn.role === 'user');
    const lastAssistant = [...turns].reverse().find(turn => turn.role === 'assistant');
    await cancelActiveResearch();
    activeThreadId.value = threadId;
    conversationTurns.value = turns;
    brief.value = lastUser?.content ?? '';
    composerBrief.value = '';
    selectedCandidateIds.value = [];
    selectedCandidate.value = null;
    isHistoryReplay.value = true;
    agentStages.value = [];
    agentForks.value = [];
    agentTrace.value = [];
    runErrorCode.value = '';
    errorMessage.value = '';
    showProcess.value = false;
    if (lastAssistant && lastUser) {
      if (!lastAssistant.terminalRunId) {
        throw new Error('历史研究缺少终态运行标识，无法恢复可信商品结果。');
      }
      const response = await replayShoppingAgentRun(lastUser.content, lastAssistant.terminalRunId);
      agentResponse.value = response;
      assistantMessage.value = response.summary;
      researchState.value = response.results.length || response.contentKind === 'CHAT_FALLBACK' ? 'complete' : 'empty';
    } else {
      agentResponse.value = null;
      assistantMessage.value = '';
      researchState.value = 'idle';
    }
    window.$message?.success('已切换到历史会话，可以继续追问。');
  } catch (error) {
    window.$message?.error(error instanceof Error ? error.message : '无法读取历史会话。');
  }
}

async function startNewConversation() {
  await cancelActiveResearch();
  activeThreadId.value = null;
  conversationTurns.value = [];
  brief.value = '';
  composerBrief.value = '';
  agentResponse.value = null;
  assistantMessage.value = '';
  agentStages.value = [];
  agentForks.value = [];
  agentTrace.value = [];
  isHistoryReplay.value = false;
  researchState.value = 'idle';
  showProcess.value = false;
  focusResearchComposer();
}

function handleComposerKeydown(event: KeyboardEvent) {
  if (event.key !== 'Enter' || event.shiftKey || event.ctrlKey || event.isComposing) return;

  event.preventDefault();
  runResearch();
}

function toCartItem(candidate: Candidate) {
  return {
    id: candidate.id,
    title: candidate.title,
    category: candidate.category,
    price: candidate.price,
    description: candidate.description,
    matches: candidate.metadata,
    visualType: 'generic' as const
  };
}

function addCandidateToCart(candidate: Candidate) {
  cartStore.add(toCartItem(candidate));
}

function addCandidateToFavorites(candidate: Candidate) {
  cartStore.addFavorite(toCartItem(candidate));
}

function isCandidateSelected(candidateId: string) {
  return selectedCandidateIds.value.includes(candidateId);
}

function toggleCandidateSelection(candidateId: string) {
  if (isCandidateSelected(candidateId)) {
    selectedCandidateIds.value = selectedCandidateIds.value.filter(id => id !== candidateId);
    return;
  }

  selectedCandidateIds.value = [...selectedCandidateIds.value, candidateId];
}

function clearCandidateSelection() {
  selectedCandidateIds.value = [];
}

function addSelectedToCart() {
  const selected = selectedCandidates.value;
  if (!selected.length) return;

  selected.forEach(({ candidate }) => addCandidateToCart(candidate));
  clearCandidateSelection();
  window.$message?.success(`已将 ${selected.length} 件商品加入本地清单。`);
}

function toggleSelectedFavorites() {
  const selected = selectedCandidates.value;
  if (!selected.length) return;

  const removeFavorites = selectedCandidatesAreFavorited.value;
  selected.forEach(({ candidate }) => {
    if (removeFavorites) {
      cartStore.removeFavorite(candidate.id);
      return;
    }

    addCandidateToFavorites(candidate);
  });

  window.$message?.success(removeFavorites ? '已取消收藏。' : `已收藏 ${selected.length} 件商品。`);
}

watch(
  () => researchHistoryStore.pendingThreadId,
  threadId => {
    if (threadId) loadHistoricalResearch(researchHistoryStore.consumePendingThreadId());
  }
);

onMounted(() => {
  loadHistoricalResearch(researchHistoryStore.consumePendingThreadId());
});
</script>

<template>
  <Teleport defer to="#header-extra">
    <div class="research-context-pill">
      <icon-material-symbols:search-insights-rounded class="text-18px text-[rgb(var(--primary-color))]" />
      <span class="research-context-label">当前研究</span>
      <span class="research-context-divider" />
      <span class="research-context-title">{{ contextTitle }}</span>
      <span class="research-context-status" :class="`is-${researchState}`">{{ contextStatus }}</span>
    </div>
  </Teleport>

  <main class="research-workspace" aria-label="购物研究工作台">
    <div class="research-scroll-content">
      <section v-if="conversationTranscriptTurns.length" class="conversation-transcript" aria-label="当前会话记录">
        <article
          v-for="turn in conversationTranscriptTurns"
          :key="turn.ordinal"
          class="conversation-turn"
          :class="`is-${turn.role}`"
        >
          <span>{{ turn.role === 'user' ? '你' : 'Glodex' }}</span>
          <p>{{ turn.content }}</p>
        </article>
      </section>

      <section class="research-process" :class="{ 'is-researching': researchState === 'researching' }">
        <button class="process-title" type="button" @click="showProcess = !showProcess">
          <span>{{ researchState === 'researching' ? 'Agent 正在研究' : '研究过程' }}</span>
          <small v-if="processSteps.length">
            {{ completedProcessCount }}/{{ processSteps.length }} 项完成
            <template v-if="correctedActionCount">，自动纠正 {{ correctedActionCount }} 次</template>
          </small>
          <icon-material-symbols:keyboard-arrow-up-rounded v-if="showProcess" class="text-18px" />
          <icon-material-symbols:keyboard-arrow-down-rounded v-else class="text-18px" />
        </button>
        <div v-show="showProcess" class="process-steps">
          <div v-if="!processSteps.length" class="process-empty">
            <icon-material-symbols:hourglass-top-rounded />
            <span>等待 Agent 返回后端签发的研究轨迹</span>
          </div>
          <template v-else>
            <div
              v-for="step in processSteps"
              :key="step.id"
              class="process-step"
              :class="{
                active: step.state === 'RUNNING',
                complete: step.state === 'FINISHED' && (!step.safeCode || step.safeCode === 'SUCCESS'),
                corrected: step.safeCode === 'INVALID_ACTION',
                failed: Boolean(step.safeCode && !['SUCCESS', 'INVALID_ACTION'].includes(step.safeCode))
              }"
            >
              <icon-material-symbols:published-with-changes-rounded v-if="step.safeCode === 'INVALID_ACTION'" />
              <icon-material-symbols:error-outline-rounded
                v-else-if="step.safeCode && !['SUCCESS', 'INVALID_ACTION'].includes(step.safeCode)"
              />
              <icon-material-symbols:check-circle-rounded v-else-if="step.state === 'FINISHED'" />
              <icon-material-symbols:hourglass-top-rounded v-else class="process-step-spinner" />
              <div class="process-step-content">
                <div class="process-step-heading">
                  <strong>{{ step.title }}</strong>
                  <span v-if="isLiveModelStream(step)" class="process-live-stream">
                    <i aria-hidden="true" />
                    Live stream
                  </span>
                  <span class="process-phase">{{ tracePhaseLabels[step.phase] }}</span>
                </div>
                <span>{{ step.detail }}</span>
                <dl v-if="step.bullets.length" class="process-facts">
                  <div v-for="bullet in step.bullets" :key="`${bullet.label}-${bullet.value}`">
                    <dt>{{ bullet.label }}</dt>
                    <dd>{{ bullet.value }}</dd>
                  </div>
                </dl>
              </div>
            </div>
          </template>
        </div>
      </section>

      <section v-if="researchState === 'researching'" class="research-loading" aria-live="polite">
        <div class="loading-copy">
          <span>{{ activeProcessStep?.title || '正在连接商品研究 Agent' }}</span>
          <span>{{ activeProcessStep?.detail || '等待后端研究轨迹' }}</span>
        </div>
        <NButton quaternary size="small" @click="cancelResearch">取消</NButton>
        <div v-if="assistantAnswer" class="assistant-live-answer">
          <p class="card-label">Agent 回复</p>
          <p>{{ assistantAnswer }}</p>
        </div>
        <div v-else class="loading-pulse" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
      </section>

      <template v-else-if="researchState === 'complete'">
        <section v-if="assistantAnswer" class="assistant-live-answer is-complete" aria-label="Agent 回复">
          <p class="card-label">Agent 回复</p>
          <p>{{ assistantAnswer }}</p>
        </section>
        <section v-if="currentProfile.candidates.length" class="candidate-section">
          <div class="candidate-section-heading">
            <div>
              <p class="research-kicker">候选商品</p>
              <h2>Agent 返回的商品</h2>
            </div>
            <span>按当前检索结果顺序展示</span>
          </div>
          <div class="candidate-grid">
            <article
              v-for="(candidate, index) in currentProfile.candidates"
              :key="candidate.id"
              class="candidate-card"
              :class="{ 'is-selected': isCandidateSelected(candidate.id) }"
            >
              <div class="candidate-image is-generic" role="img" :aria-label="candidate.title">
                <div class="candidate-artwork" aria-hidden="true">
                  <icon-material-symbols:inventory-2-outline-rounded />
                  <span>{{ candidate.source }}</span>
                </div>
              </div>
              <div class="candidate-content">
                <div class="candidate-meta">
                  <span>{{ candidate.category }}</span>
                  <span>结果 {{ index + 1 }}</span>
                </div>
                <h3>{{ candidate.title }}</h3>
                <strong class="candidate-price">{{ candidate.price }}</strong>
                <p>{{ candidate.description }}</p>
                <ul class="candidate-matches">
                  <li v-for="metadata in candidate.metadata.slice(0, 2)" :key="metadata">
                    <icon-material-symbols:info-outline-rounded />
                    {{ metadata }}
                  </li>
                </ul>
                <div class="candidate-actions">
                  <NButton
                    size="small"
                    :type="isCandidateSelected(candidate.id) ? 'primary' : 'default'"
                    :secondary="!isCandidateSelected(candidate.id)"
                    @click="toggleCandidateSelection(candidate.id)"
                  >
                    <template #icon>
                      <icon-material-symbols:check-rounded v-if="isCandidateSelected(candidate.id)" />
                      <icon-material-symbols:add-rounded v-else />
                    </template>
                    {{ isCandidateSelected(candidate.id) ? '已选择' : '选择' }}
                  </NButton>
                  <NButton text type="primary" size="small" @click="selectedCandidate = candidate">
                    查看商品信息
                    <template #icon>
                      <icon-material-symbols:arrow-forward-rounded />
                    </template>
                  </NButton>
                </div>
              </div>
            </article>
          </div>
        </section>
      </template>
      <section v-else-if="researchState === 'empty'" class="research-feedback" aria-live="polite">
        <icon-material-symbols:search-off-rounded />
        <div>
          <h2>没有找到可展示的商品</h2>
          <p>{{ assistantAnswer || '服务已完成检索，但没有返回结果。请换一个商品名、品牌或更短的关键词。' }}</p>
        </div>
        <NButton secondary type="primary" @click="focusResearchComposer(true)">修改需求</NButton>
      </section>
      <section v-else-if="researchState === 'error'" class="research-feedback is-error" aria-live="assertive">
        <icon-material-symbols:cloud-off-rounded />
        <div>
          <h2>商品研究 Agent 未完成</h2>
          <p>{{ errorMessage }}</p>
        </div>
        <NButton secondary type="primary" @click="runResearch(brief)">重试</NButton>
      </section>
    </div>

    <section v-if="selectedCandidateCount" class="selection-tray" aria-live="polite">
      <div class="selection-tray-summary">
        <button class="selection-clear" type="button" aria-label="取消选择" @click="clearCandidateSelection">
          <icon-material-symbols:close-rounded />
        </button>
        <strong>已选 {{ selectedCandidateCount }} 件</strong>
      </div>
      <div class="selection-tray-actions">
        <NButton
          size="small"
          secondary
          type="primary"
          circle
          :aria-label="selectedCandidatesAreFavorited ? '取消收藏' : '收藏选中的商品'"
          @click="toggleSelectedFavorites"
        >
          <template #icon>
            <icon-material-symbols:favorite-rounded v-if="selectedCandidatesAreFavorited" />
            <icon-material-symbols:favorite-outline-rounded v-else />
          </template>
        </NButton>
        <NButton size="small" secondary type="primary" @click="addSelectedToCart">
          <template #icon>
            <icon-material-symbols:shopping-cart-outline-rounded />
          </template>
          加入本地清单
        </NButton>
      </div>
    </section>

    <section class="research-composer" aria-label="开始新的购物研究">
      <label for="research-composer-input" class="composer-label">告诉 Glodex 你要找什么</label>
      <div class="research-composer-shell">
        <textarea
          id="research-composer-input"
          ref="composerInput"
          v-model="composerBrief"
          rows="1"
          placeholder="例如：预算 3,000 元左右的游戏手机，性能和续航要好。"
          @input="resizeComposer"
          @keydown="handleComposerKeydown"
        />
        <NButton
          class="research-composer-send"
          size="small"
          circle
          type="primary"
          :disabled="!composerBrief.trim() || researchState === 'researching'"
          aria-label="开始研究"
          @click="runResearch()"
        >
          <template #icon>
            <icon-material-symbols:arrow-upward-rounded class="text-16px" />
          </template>
        </NButton>
      </div>
      <div class="research-composer-meta">
        <span>
          {{ activeThreadId ? '本次提问会继续当前会话，并读取相关长期记忆。' : '将创建新会话，并读取相关长期记忆。' }}
        </span>
        <button v-if="activeThreadId" type="button" class="new-conversation-button" @click="startNewConversation">
          新会话
        </button>
        <span v-else>Enter 开始研究 · Shift+Enter 换行</span>
      </div>
    </section>
  </main>

  <NModal
    v-model:show="isEvidenceVisible"
    preset="card"
    class="evidence-modal"
    title="商品信息"
  >
    <div v-if="selectedCandidate" class="product-detail">
      <header class="product-detail-header">
        <div class="product-detail-tags">
          <span>{{ selectedCandidate.source }}</span>
          <span>{{ selectedCandidate.category }}</span>
        </div>
        <h2>{{ selectedCandidate.title }}</h2>
        <strong>{{ selectedCandidate.price }}</strong>
      </header>

      <section class="product-detail-section" aria-labelledby="requirement-coverage-title">
        <div class="product-detail-section-heading">
          <div>
            <h3 id="requirement-coverage-title">验证结论</h3>
            <p>每项结论都展示实际依据和仍缺信息</p>
          </div>
        </div>
        <div v-if="selectedCandidate.requirements.length" class="requirement-list">
          <div
            v-for="requirement in selectedCandidate.requirements"
            :key="`${requirement.label}-${requirement.importance}`"
            class="requirement-row"
            :class="`is-${requirement.status.toLowerCase()}`"
          >
            <div class="requirement-copy">
              <icon-material-symbols:check-circle-rounded v-if="requirement.status === 'VERIFIED'" />
              <icon-material-symbols:help-outline-rounded v-else />
              <div>
                <strong>{{ requirement.label }}</strong>
                <span>{{ requirement.importance === 'required' ? '必要条件' : '偏好条件' }}</span>
                <dl class="requirement-evidence-summary">
                  <div v-if="requirement.knownFacts.length">
                    <dt>已知证据</dt>
                    <dd>{{ requirement.knownFacts.join('；') }}</dd>
                  </div>
                  <div v-if="requirement.missingEvidence">
                    <dt>仍缺证据</dt>
                    <dd>{{ requirement.missingEvidence }}</dd>
                  </div>
                  <div>
                    <dt>当前结论</dt>
                    <dd>{{ requirement.conclusion }}</dd>
                  </div>
                </dl>
                <div v-if="requirement.evidence.length" class="requirement-evidence-links">
                  <a
                    v-for="source in groupEvidenceBySource(requirement.evidence)"
                    :key="source.key"
                    :href="source.sourceUrl"
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    {{ source.fieldSummary }} · {{ source.sourceLabel }}
                  </a>
                </div>
              </div>
            </div>
            <span class="requirement-status" :class="`is-${requirement.status.toLowerCase()}`">
              {{ requirement.statusLabel }}
            </span>
          </div>
        </div>
        <p v-else class="product-detail-empty">当前需求没有可单独判断的条件。</p>
      </section>

      <section class="product-detail-section" aria-labelledby="product-overview-title">
        <div class="product-detail-section-heading">
          <div>
            <h3 id="product-overview-title">商品概览</h3>
            <p>Agent 最终用于比较的商品与报价</p>
          </div>
        </div>
        <dl class="product-overview">
          <div>
            <dt>商品来源</dt>
            <dd>{{ selectedCandidate.source }}</dd>
          </div>
          <div>
            <dt>商品类目</dt>
            <dd>{{ selectedCandidate.category }}</dd>
          </div>
          <div>
            <dt>到手价</dt>
            <dd>{{ selectedCandidate.price }}</dd>
          </div>
          <div>
            <dt>可核验来源</dt>
            <dd>{{ selectedEvidenceSources.length }} 个</dd>
          </div>
        </dl>
      </section>

      <section
        v-if="selectedEvidenceSources.length"
        class="product-detail-section"
        aria-labelledby="evidence-source-title"
      >
        <div class="product-detail-section-heading">
          <div>
            <h3 id="evidence-source-title">证据来源</h3>
            <p>同一网页只展示一次，点击可核对厂商规格或公开价格来源</p>
          </div>
        </div>
        <div class="evidence-source-list">
          <a
            v-for="source in selectedEvidenceSources"
            :key="source.key"
            :href="source.sourceUrl"
            target="_blank"
            rel="noopener noreferrer"
          >
            <div>
              <strong>{{ source.sourceLabel }}</strong>
              <span>{{ source.fieldSummary }}</span>
            </div>
            <small>
              {{ source.hostname }}
              <icon-material-symbols:open-in-new-rounded />
            </small>
          </a>
        </div>
      </section>
    </div>
  </NModal>
</template>

<style scoped>
.research-context-pill {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 330px;
  height: 42px;
  padding: 0 15px;
  border: 1px solid rgba(99, 102, 241, 0.1);
  border-radius: 999px;
  background: rgb(255 255 255 / 0.94);
  box-shadow: 0 10px 26px rgb(72 84 111 / 0.1);
  color: #303445;
  backdrop-filter: blur(12px);
}

.research-context-label,
.research-context-status {
  font-size: 12px;
  font-weight: 600;
}

.research-context-label {
  color: #747b91;
}

.research-context-divider {
  width: 1px;
  height: 16px;
  background: #e6e8ef;
}

.research-context-title {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  font-size: 13px;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.research-context-status {
  flex: 0 0 auto;
  margin-left: auto;
  padding: 4px 8px;
  border-radius: 999px;
  background: #eef0ff;
  color: rgb(var(--primary-color));
  white-space: nowrap;
}

.research-context-status.is-complete {
  background: #eef7ef;
  color: #4d7d58;
}

.research-context-status.is-researching {
  background: #f3f0ff;
  color: #6254c7;
}

.research-context-status.is-empty {
  background: #fff7e7;
  color: #9b7438;
}

.research-context-status.is-error {
  background: #fff0f0;
  color: #b15050;
}

.research-workspace {
  display: flex;
  height: 100%;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
  border-radius: 20px;
  background: #f5f8fc;
  color: #272a36;
}

.research-scroll-content {
  min-height: 0;
  flex: 1;
  overflow: auto;
  padding: 22px 34px 18px;
}

.conversation-transcript {
  display: grid;
  gap: 10px;
  margin-bottom: 16px;
}

.conversation-turn {
  display: grid;
  max-width: min(78%, 720px);
  gap: 4px;
}

.conversation-turn > span {
  padding: 0 4px;
  color: #81889a;
  font-size: 11px;
  font-weight: 700;
}

.conversation-turn p {
  margin: 0;
  padding: 11px 14px;
  border: 1px solid #e5e9f0;
  border-radius: 15px 15px 15px 5px;
  background: #fff;
  color: #4c5364;
  font-size: 13px;
  line-height: 1.65;
  white-space: pre-wrap;
}

.conversation-turn.is-user {
  justify-self: end;
}

.conversation-turn.is-user > span {
  text-align: right;
}

.conversation-turn.is-user p {
  border-color: rgb(var(--primary-color) / 0.16);
  border-radius: 15px 15px 5px;
  background: rgb(var(--primary-color) / 0.08);
  color: #42475a;
}

.candidate-section-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
}

.research-kicker,
.card-label {
  margin: 0 0 6px;
  color: rgb(var(--primary-color));
  font-size: 12px;
  font-weight: 700;
}

.candidate-section h2 {
  margin: 0;
  color: #222532;
  font-size: 27px;
  font-weight: 700;
  letter-spacing: -0.035em;
  line-height: 1.18;
}

.research-process,
.candidate-card,
.research-loading,
.research-feedback {
  border: 1px solid #e6eaf1;
  border-radius: 18px;
  background: #fff;
  box-shadow: 0 12px 30px rgb(61 73 106 / 0.06);
}

.research-composer {
  flex: 0 0 auto;
  margin: 0 34px 20px;
  padding: 12px 14px 10px;
  border: 1px solid #dfe4ed;
  border-radius: 17px;
  background: #fff;
  box-shadow: 0 10px 26px rgb(61 73 106 / 0.08);
}

.composer-label {
  display: block;
  margin: 0 0 7px;
  color: #5d6576;
  font-size: 12px;
  font-weight: 650;
}

.research-composer-shell {
  display: flex;
  align-items: flex-end;
  gap: 10px;
}

.research-composer-shell textarea {
  width: 100%;
  height: 42px;
  min-height: 42px;
  max-height: 112px;
  padding: 9px 2px;
  box-sizing: border-box;
  resize: none;
  border: 0;
  background: transparent;
  color: #303442;
  caret-color: rgb(var(--primary-color));
  font: inherit;
  font-size: 14px;
  line-height: 1.55;
  outline: 0;
}

.research-composer-shell textarea::placeholder {
  color: #979dad;
}

.research-composer:focus-within {
  border-color: rgb(var(--primary-color) / 0.38);
  box-shadow:
    0 0 0 3px rgb(var(--primary-color) / 0.08),
    0 10px 26px rgb(61 73 106 / 0.1);
}

.research-composer-send {
  flex: 0 0 auto;
  margin-bottom: 3px;
}

.research-composer-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding-top: 6px;
  color: #8a91a0;
  font-size: 11px;
}

.new-conversation-button {
  flex: 0 0 auto;
  padding: 0;
  border: 0;
  background: transparent;
  color: rgb(var(--primary-color));
  cursor: pointer;
  font: inherit;
  font-weight: 700;
}

.research-process {
  padding: 14px 18px;
}

.process-title {
  display: flex;
  width: 100%;
  align-items: center;
  gap: 4px;
  padding: 0;
  border: 0;
  background: transparent;
  color: #363a48;
  cursor: pointer;
  font-size: 14px;
  font-weight: 700;
  text-align: left;
}

.process-title small {
  margin-left: auto;
  color: #858b9b;
  font-size: 11px;
  font-weight: 550;
}

.process-title > svg {
  margin-left: 6px;
}

.process-steps {
  display: grid;
  max-height: 320px;
  overflow: auto;
  padding-top: 10px;
}

.process-step {
  display: flex;
  align-items: flex-start;
  gap: 11px;
  min-width: 0;
  padding: 11px 4px;
  border-top: 1px solid #edf0f5;
  color: #8a91a2;
}

.process-step.active {
  color: rgb(var(--primary-color));
}

.process-empty {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 1px;
  color: #8a91a2;
  font-size: 12px;
}

.process-empty svg {
  color: rgb(var(--primary-color));
  font-size: 17px;
}

.process-step > svg,
.process-step-spinner {
  display: flex;
  width: 24px;
  height: 24px;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  margin-top: 1px;
  font-size: 20px;
}

.process-step.complete > svg {
  color: #4b8057;
}

.process-step.corrected > svg {
  color: #a66f21;
}

.process-step.failed > svg {
  color: #b15050;
}

.process-step.active > svg {
  color: rgb(var(--primary-color));
}

.process-step-content {
  display: grid;
  flex: 1;
  gap: 4px;
  min-width: 0;
}

.process-step-heading {
  display: flex;
  align-items: center;
  gap: 8px;
}

.process-step-heading strong {
  color: #464b5a;
  font-size: 13px;
}

.process-phase {
  padding: 1px 6px;
  border-radius: 999px;
  background: rgb(var(--primary-color) / 0.08);
  color: rgb(var(--primary-color));
  font-size: 9px;
  font-weight: 750;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.process-live-stream {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 6px;
  border: 1px solid rgb(var(--primary-color) / 0.22);
  border-radius: 999px;
  background: rgb(var(--primary-color) / 0.06);
  color: rgb(var(--primary-color));
  font-size: 9px;
  font-weight: 800;
  letter-spacing: 0.04em;
  line-height: 1;
  text-transform: uppercase;
  white-space: nowrap;
}

.process-live-stream i {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentcolor;
}

.process-step-content > span {
  color: #777f90;
  font-size: 12px;
  line-height: 1.55;
}

.process-facts {
  display: grid;
  gap: 3px;
  margin: 4px 0 0;
}

.process-facts > div {
  display: grid;
  grid-template-columns: minmax(62px, auto) minmax(0, 1fr);
  gap: 8px;
  padding-left: 9px;
  border-left: 2px solid rgb(var(--primary-color) / 0.16);
  font-size: 11px;
  line-height: 1.5;
}

.process-facts dt {
  color: #707789;
  font-weight: 700;
}

.process-facts dd {
  min-width: 0;
  margin: 0;
  color: #555d6e;
  overflow-wrap: anywhere;
}

@media (prefers-reduced-motion: no-preference) {
  .process-step-spinner {
    animation: process-pulse 1.1s ease-in-out infinite;
  }

  .process-live-stream i {
    animation: live-stream-pulse 1s ease-in-out infinite;
  }
}

@keyframes process-pulse {
  50% {
    opacity: 0.48;
    transform: translateY(1px);
  }
}

@keyframes live-stream-pulse {
  50% {
    opacity: 0.35;
    transform: scale(0.72);
  }
}

.candidate-section {
  padding-top: 18px;
}

.candidate-section-heading {
  align-items: flex-end;
  padding-bottom: 11px;
}

.candidate-section h2 {
  font-size: 21px;
}

.candidate-section-heading > span {
  padding-bottom: 3px;
  color: #8b91a0;
  font-size: 12px;
}

.candidate-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.15fr) repeat(2, minmax(0, 1fr));
  gap: 14px;
}

.candidate-card {
  display: flex;
  min-width: 0;
  flex-direction: column;
  overflow: hidden;
  transition:
    transform 0.18s ease,
    box-shadow 0.18s ease;
}

.candidate-card:hover {
  box-shadow: 0 16px 34px rgb(61 73 106 / 0.1);
  transform: translateY(-2px);
}

.candidate-card.is-selected {
  border-color: rgb(var(--primary-color) / 0.46);
  box-shadow:
    0 0 0 2px rgb(var(--primary-color) / 0.08),
    0 12px 30px rgb(61 73 106 / 0.08);
}

.candidate-image {
  height: 126px;
  border-bottom: 1px solid #edf0f5;
}

.candidate-image.is-generic {
  display: grid;
  place-items: center;
  overflow: hidden;
  background:
    radial-gradient(circle at 84% 18%, rgb(255 255 255 / 0.58) 0 13%, transparent 13.5%),
    linear-gradient(135deg, #edf0ff, #dfe7fb);
}

.candidate-artwork {
  display: flex;
  align-items: center;
  gap: 10px;
  color: #6870a7;
  font-size: 13px;
  font-weight: 700;
}

.candidate-artwork svg {
  font-size: 34px;
}

.candidate-content {
  display: flex;
  min-height: 0;
  flex: 1;
  flex-direction: column;
  padding: 15px 16px 14px;
}

.candidate-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  color: #777f92;
  font-size: 11px;
  font-weight: 650;
}

.candidate-meta span:first-child {
  color: rgb(var(--primary-color));
}

.candidate-card h3 {
  margin: 8px 0 10px;
  color: #303442;
  font-size: 16px;
  line-height: 1.32;
}

.candidate-price {
  color: #282b37;
  font-size: 18px;
  letter-spacing: -0.02em;
}

.candidate-content > p {
  min-height: 42px;
  margin: 8px 0 10px;
  color: #747b8b;
  font-size: 12px;
  line-height: 1.65;
}

.candidate-matches {
  display: grid;
  gap: 5px;
  margin: 0;
  padding: 0;
  color: #606879;
  font-size: 12px;
  line-height: 1.4;
  list-style: none;
}

.candidate-matches li {
  display: flex;
  gap: 4px;
  align-items: center;
}

.candidate-matches svg {
  color: rgb(var(--primary-color));
  font-size: 15px;
}

.candidate-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: auto;
  padding-top: 14px;
}

.selection-tray {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  margin: 0 34px 10px;
  padding: 8px 9px 8px 8px;
  border: 1px solid #dfe4ed;
  border-radius: 15px;
  background: #fff;
  box-shadow: 0 8px 22px rgb(61 73 106 / 0.07);
}

.selection-tray-summary,
.selection-tray-actions {
  display: flex;
  align-items: center;
}

.selection-tray-summary {
  min-width: 0;
  gap: 8px;
}

.selection-tray-summary strong {
  color: #3d4251;
  font-size: 13px;
  white-space: nowrap;
}

.selection-clear {
  display: grid;
  width: 29px;
  height: 29px;
  place-items: center;
  padding: 0;
  border: 0;
  border-radius: 9px;
  background: #f3f5f9;
  color: #737b8c;
  cursor: pointer;
  transition:
    background 0.18s ease,
    color 0.18s ease;
}

.selection-clear:hover {
  background: #eceeff;
  color: rgb(var(--primary-color));
}

.selection-clear svg {
  font-size: 17px;
}

.selection-tray-actions {
  justify-content: flex-end;
  gap: 8px;
}

.research-loading {
  margin-top: 20px;
  padding: 22px;
}

.research-feedback {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-top: 20px;
  padding: 22px;
}

.research-feedback > svg {
  flex: 0 0 auto;
  color: rgb(var(--primary-color));
  font-size: 30px;
}

.research-feedback > div {
  min-width: 0;
  flex: 1;
}

.research-feedback h2 {
  margin: 0;
  color: #353a49;
  font-size: 16px;
}

.research-feedback p {
  margin: 5px 0 0;
  color: #737b8b;
  font-size: 13px;
  line-height: 1.6;
}

.research-feedback.is-error > svg {
  color: #b15050;
}

.loading-copy {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 18px;
  color: #596071;
  font-size: 14px;
  font-weight: 650;
}

.loading-copy span:last-child {
  color: #9097a8;
  font-size: 12px;
  font-weight: 500;
}

.loading-pulse {
  display: flex;
  gap: 7px;
}

.loading-pulse span {
  width: 34px;
  height: 7px;
  border-radius: 8px;
  background: linear-gradient(90deg, #f1f3f8 25%, #f8f9fc 50%, #f1f3f8 75%);
  background-size: 200% 100%;
  animation: loading-shimmer 1.2s ease-in-out infinite;
}

.loading-pulse span:nth-child(2) {
  animation-delay: 0.12s;
}

.loading-pulse span:nth-child(3) {
  animation-delay: 0.24s;
}

.assistant-live-answer {
  padding: 13px 14px;
  border: 1px solid #e6e8fb;
  border-radius: 13px;
  background: #fafaff;
}

.assistant-live-answer.is-complete {
  margin-bottom: 14px;
}

.assistant-live-answer .card-label {
  margin-bottom: 5px;
}

.assistant-live-answer p:last-child {
  margin: 0;
  color: #555c6d;
  font-size: 13px;
  line-height: 1.65;
  white-space: pre-wrap;
}

:global(.evidence-modal) {
  width: min(720px, calc(100vw - 48px));
}

.product-detail {
  display: grid;
  gap: 20px;
}

.product-detail-header {
  padding-bottom: 20px;
  border-bottom: 1px solid #eceff5;
}

.product-detail-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}

.product-detail-tags span {
  padding: 4px 9px;
  border-radius: 999px;
  background: #f0f1ff;
  color: #6265c8;
  font-size: 12px;
  font-weight: 650;
}

.product-detail-header h2 {
  margin: 12px 0 10px;
  color: #252936;
  font-size: 18px;
  font-weight: 700;
  line-height: 1.45;
  overflow-wrap: anywhere;
}

.product-detail-header > strong {
  color: rgb(var(--primary-color));
  font-size: 21px;
}

.product-detail-section {
  display: grid;
  gap: 12px;
}

.product-detail-section-heading h3 {
  margin: 0;
  color: #353a49;
  font-size: 15px;
}

.product-detail-section-heading p {
  margin: 3px 0 0;
  color: #858c9c;
  font-size: 12px;
}

.requirement-list {
  overflow: hidden;
  border: 1px solid #e8ebf2;
  border-radius: 13px;
}

.requirement-row {
  display: flex;
  min-height: 58px;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 11px 14px;
  background: #fafbfe;
}

.requirement-row + .requirement-row {
  border-top: 1px solid #e8ebf2;
}

.requirement-copy {
  display: flex;
  min-width: 0;
  align-items: flex-start;
  gap: 10px;
}

.requirement-copy > svg {
  width: 19px;
  height: 19px;
  flex: 0 0 auto;
  margin-top: 2px;
  color: #8a91a1;
}

.requirement-row.is-verified .requirement-copy > svg {
  color: #4d895c;
}

.requirement-row.is-not_matched .requirement-copy > svg {
  color: #b95555;
}

.requirement-copy strong,
.requirement-copy span {
  display: block;
}

.requirement-copy strong {
  color: #353a49;
  font-size: 13px;
  line-height: 1.4;
}

.requirement-copy span {
  margin-top: 2px;
  color: #8a91a1;
  font-size: 11px;
}

.requirement-evidence-summary {
  display: grid;
  gap: 5px;
  max-width: 52ch;
  margin: 7px 0 0;
}

.requirement-evidence-summary > div {
  display: grid;
  grid-template-columns: 58px minmax(0, 1fr);
  gap: 7px;
}

.requirement-evidence-summary dt {
  color: #8a91a1;
  font-size: 11px;
}

.requirement-evidence-summary dd {
  margin: 0;
  color: #626979;
  font-size: 12px;
  line-height: 1.55;
}

.requirement-evidence-links {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 7px;
}

.requirement-evidence-links a {
  padding: 3px 7px;
  border: 1px solid #dfe3f0;
  border-radius: 7px;
  color: rgb(var(--primary-color));
  font-size: 10px;
  text-decoration: none;
}

.requirement-evidence-links a:hover {
  border-color: rgb(var(--primary-color) / 0.45);
  background: rgb(var(--primary-color) / 0.05);
}

.requirement-status {
  flex: 0 0 auto;
  padding: 4px 9px;
  border-radius: 999px;
  background: #f0f1f5;
  color: #707787;
  font-size: 12px;
  font-weight: 650;
}

.requirement-status.is-verified {
  background: #eaf5ed;
  color: #3f7b4e;
}

.requirement-status.is-not_matched {
  background: #faeeee;
  color: #a64747;
}

.requirement-status.is-conflicted,
.requirement-status.is-insufficient {
  background: #fff5df;
  color: #97651b;
}

.product-detail-empty {
  margin: 0;
  color: #858c9c;
  font-size: 13px;
}

.product-overview {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1px;
  overflow: hidden;
  margin: 0;
  border: 1px solid #e8ebf2;
  border-radius: 13px;
  background: #e8ebf2;
}

.product-overview > div {
  min-width: 0;
  padding: 12px 14px;
  background: #fafbfe;
}

.product-overview dt {
  color: #8a91a1;
  font-size: 11px;
}

.product-overview dd {
  margin: 4px 0 0;
  color: #353a49;
  font-size: 13px;
  font-weight: 650;
  overflow-wrap: anywhere;
}

.evidence-source-list {
  display: grid;
  gap: 8px;
}

.evidence-source-list a {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  padding: 12px 14px;
  border: 1px solid #e8ebf2;
  border-radius: 11px;
  background: #fafbfe;
  color: inherit;
  text-decoration: none;
}

.evidence-source-list a:hover {
  border-color: rgb(var(--primary-color) / 0.35);
}

.evidence-source-list strong {
  display: block;
  color: #353a49;
  font-size: 13px;
}

.evidence-source-list span {
  display: block;
  margin-top: 3px;
  color: #858c9c;
  font-size: 12px;
  line-height: 1.45;
}

.evidence-source-list small {
  display: inline-flex;
  align-items: center;
  flex: 0 0 auto;
  gap: 5px;
  color: #747b91;
  font-size: 11px;
}

.evidence-source-list small svg {
  font-size: 14px;
}

@keyframes loading-shimmer {
  from {
    background-position: 100% 0;
  }
  to {
    background-position: -100% 0;
  }
}

.dark .research-context-pill {
  border-color: rgb(255 255 255 / 0.08);
  background: rgb(34 34 34 / 0.95);
  box-shadow: 0 10px 26px rgb(0 0 0 / 0.2);
  color: #e5e7eb;
}

.dark .research-context-label,
.dark .research-context-title,
.dark .candidate-section h2,
.dark .candidate-card h3,
.dark .candidate-price {
  color: #e6e8ef;
}

.dark .research-context-divider {
  background: #3d4250;
}

.dark .research-workspace {
  background: #17191f;
  color: #e5e7eb;
}

.dark .research-process,
.dark .candidate-card,
.dark .research-loading,
.dark .research-feedback {
  border-color: #30333c;
  background: #202228;
  box-shadow: none;
}

.dark .research-composer {
  border-color: #30333c;
  background: #202228;
  box-shadow: none;
}

.dark .selection-tray {
  border-color: #30333c;
  background: #202228;
  box-shadow: none;
}

.dark .selection-tray-summary strong {
  color: #e6e8ef;
}

.dark .selection-clear {
  background: #2a2d35;
  color: #a8afbe;
}

.dark .composer-label,
.dark .research-composer-meta,
.dark .research-composer-shell textarea::placeholder {
  color: #9ea5b4;
}

.dark .research-composer-shell textarea {
  color: #e6e8ef;
}

.dark .candidate-content > p,
.dark .research-feedback p,
.dark .candidate-matches {
  color: #b0b6c4;
}

.dark .process-step,
.dark .assistant-live-answer {
  border-color: #353842;
}

.dark .process-step.active {
  color: rgb(var(--primary-color));
}

.dark .process-title,
.dark .process-step strong {
  color: #e1e4eb;
}

.dark .process-title small,
.dark .process-step div > span {
  color: #aeb4c2;
}

.dark .assistant-live-answer p:last-child {
  color: #c2c7d2;
}

.dark .conversation-turn p {
  border-color: #353842;
  background: #202228;
  color: #c2c7d2;
}

.dark .conversation-turn.is-user p {
  border-color: rgb(var(--primary-color) / 0.3);
  background: rgb(var(--primary-color) / 0.13);
  color: #e1e4eb;
}

.dark .candidate-image {
  border-color: #30333c;
}

.dark .product-detail-header {
  border-color: #353842;
}

.dark .product-detail-header h2,
.dark .product-detail-section-heading h3,
.dark .requirement-copy strong,
.dark .product-overview dd {
  color: #e1e4eb;
}

.dark .requirement-evidence-summary dd {
  color: #c2c7d2;
}

.dark .evidence-source-list strong {
  color: #e1e4eb;
}

.dark .product-detail-tags span {
  background: #303147;
  color: #b9baf7;
}

.dark .requirement-list,
.dark .product-overview {
  border-color: #353842;
  background: #353842;
}

.dark .requirement-row,
.dark .product-overview > div,
.dark .evidence-source-list a {
  background: #292c34;
}

.dark .evidence-source-list a,
.dark .requirement-evidence-links a {
  border-color: #353842;
}

.dark .requirement-row + .requirement-row {
  border-color: #353842;
}

@media (prefers-reduced-motion: reduce) {
  .candidate-card,
  .loading-pulse span {
    animation: none;
    transition: none;
  }
}

@media (max-width: 1024px) {
  .candidate-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .candidate-card:last-child {
    grid-column: 1 / -1;
  }
}

@media (max-width: 760px) {
  :global(.evidence-modal) {
    width: calc(100vw - 24px);
  }

  .research-workspace {
    border-radius: 16px;
  }

  .research-scroll-content {
    padding: 22px 18px 14px;
  }

  .research-composer {
    margin: 0 18px 16px;
    padding: 11px 12px 9px;
  }

  .selection-tray {
    gap: 8px;
    margin: 0 18px 9px;
    padding: 8px;
  }

  .selection-tray-actions {
    gap: 6px;
  }

  .selection-tray-actions .n-button {
    padding-right: 10px;
    padding-left: 10px;
  }

  .research-composer-meta span:first-child {
    display: none;
  }

  .research-composer-meta {
    justify-content: flex-end;
  }

  .conversation-turn {
    max-width: 92%;
  }

  .research-context-pill {
    min-width: 0;
    max-width: calc(100vw - 160px);
  }

  .research-context-label,
  .research-context-divider {
    display: none;
  }

  .candidate-section-heading {
    align-items: flex-start;
    flex-direction: column;
  }

  .candidate-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .candidate-card:last-child {
    grid-column: auto;
  }

  .candidate-section-heading > span {
    padding: 0;
  }

  .candidate-actions {
    align-items: flex-start;
    flex-direction: column;
  }

  .product-overview {
    grid-template-columns: minmax(0, 1fr);
  }

  .evidence-source-list a {
    align-items: flex-start;
    flex-direction: column;
    gap: 8px;
  }

  .requirement-row {
    align-items: flex-start;
  }
}
</style>
