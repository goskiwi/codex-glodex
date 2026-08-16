<script setup lang="ts">
import { onMounted, ref } from 'vue';
import type { ConversationThread, ConversationTurn } from '@/service/memory';
import { useResearchHistoryStore } from '@/store/modules/research-history';
import { useRouterPush } from '@/hooks/common/router';

defineOptions({
  name: 'ChatHistory'
});

const { routerPushByKey } = useRouterPush();
const researchHistoryStore = useResearchHistoryStore();
const previews = ref<Record<string, ConversationTurn[]>>({});

function latestUserTurn(threadId: string) {
  return [...(previews.value[threadId] ?? [])].reverse().find(turn => turn.role === 'user')?.content ?? '购物研究会话';
}

function lastAssistantTurn(threadId: string) {
  return (
    [...(previews.value[threadId] ?? [])].reverse().find(turn => turn.role === 'assistant')?.content ?? '等待继续研究'
  );
}

function reopenResearch(item: ConversationThread) {
  researchHistoryStore.open(item.threadId);
  routerPushByKey('chat');
}

async function removeResearch(item: ConversationThread) {
  try {
    await researchHistoryStore.remove(item.threadId);
    previews.value = Object.fromEntries(
      Object.entries(previews.value).filter(([threadId]) => threadId !== item.threadId)
    );
    window.$message?.success('会话已删除。');
  } catch (error) {
    window.$message?.error(error instanceof Error ? error.message : '删除失败。');
  }
}

onMounted(async () => {
  try {
    await researchHistoryStore.load();
    const entries = await Promise.all(
      researchHistoryStore.threads.map(
        async thread => [thread.threadId, await researchHistoryStore.turns(thread.threadId)] as const
      )
    );
    previews.value = Object.fromEntries(entries);
  } catch (error) {
    window.$message?.error(error instanceof Error ? error.message : '无法读取研究历史。');
  }
});
</script>

<template>
  <main class="history-workspace" aria-label="研究历史">
    <section class="history-panel">
      <header class="history-heading">
        <div>
          <p>研究历史</p>
          <h1>服务端研究会话</h1>
          <span>{{ researchHistoryStore.count }} 个可继续的会话</span>
        </div>
        <NButton secondary type="primary" @click="routerPushByKey('chat')">
          开始研究
          <template #icon>
            <icon-material-symbols:arrow-forward-rounded />
          </template>
        </NButton>
      </header>

      <section v-if="researchHistoryStore.count" class="history-list" aria-label="历史研究列表">
        <article v-for="item in researchHistoryStore.threads" :key="item.threadId" class="history-row">
          <button
            class="history-open"
            type="button"
            :aria-label="`继续${latestUserTurn(item.threadId)}会话`"
            @click="reopenResearch(item)"
          >
            <div class="history-row-icon" aria-hidden="true">
              <icon-material-symbols:search-insights-rounded />
            </div>
            <div class="history-row-copy">
              <div class="history-row-title">
                <h2>{{ latestUserTurn(item.threadId) }}</h2>
                <time>{{ item.threadId.slice(-10) }}</time>
              </div>
              <p>{{ lastAssistantTurn(item.threadId) }}</p>
              <div class="history-row-meta">
                <span>{{ item.turnCount }} 条消息</span>
                <span>可继续追问</span>
              </div>
            </div>
            <icon-material-symbols:arrow-forward-rounded class="history-row-arrow" aria-hidden="true" />
          </button>
          <button
            class="history-remove"
            type="button"
            :aria-label="`删除${latestUserTurn(item.threadId)}会话`"
            @click="removeResearch(item)"
          >
            删除
          </button>
        </article>
      </section>

      <section v-else class="history-empty">
        <div class="history-empty-icon">
          <icon-material-symbols:history-rounded />
        </div>
        <h2>{{ researchHistoryStore.loading ? '正在读取研究历史' : '还没有研究会话' }}</h2>
        <p>研究消息只从服务端读取，不再保存浏览器副本。</p>
        <NButton type="primary" @click="routerPushByKey('chat')">开始第一项研究</NButton>
      </section>
    </section>
  </main>
</template>

<style scoped>
.history-workspace {
  display: flex;
  height: 100%;
  min-height: 0;
  padding: 20px 28px 28px !important;
  background: #f5f8fc;
  color: #292d39;
}

.history-panel {
  display: flex;
  width: min(900px, 100%);
  min-height: 0;
  flex: 1;
  flex-direction: column;
  margin: 0 auto;
  overflow: hidden;
  border: 1px solid #e4e8ef;
  border-radius: 20px;
  background: #fff;
  box-shadow: 0 14px 34px rgb(76 88 118 / 0.08);
}

.history-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  padding: 26px 28px 20px;
  border-bottom: 1px solid #e9edf3;
}

.history-heading p {
  margin: 0 0 6px;
  color: rgb(var(--primary-color));
  font-size: 12px;
  font-weight: 700;
}

.history-heading h1 {
  margin: 0;
  color: #303440;
  font-size: 23px;
  letter-spacing: -0.035em;
  line-height: 1.2;
}

.history-heading span {
  display: block;
  margin-top: 7px;
  color: #8a91a0;
  font-size: 12px;
}

.history-list {
  min-height: 0;
  flex: 1;
  overflow-y: auto;
  padding: 14px 18px 18px;
}

.history-row {
  position: relative;
  overflow: hidden;
  border: 1px solid #e8ecf2;
  border-radius: 15px;
  background: #fff;
  transition:
    border-color 160ms ease,
    box-shadow 160ms ease,
    transform 160ms ease;
}

.history-row + .history-row {
  margin-top: 11px;
}

.history-row:hover {
  border-color: rgb(var(--primary-color) / 0.32);
  box-shadow: 0 9px 20px rgb(80 88 157 / 0.07);
  transform: translateY(-1px);
}

.history-open {
  display: grid;
  width: 100%;
  grid-template-columns: 42px minmax(0, 1fr) 20px;
  align-items: center;
  gap: 13px;
  padding: 16px 68px 16px 16px;
  border: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
  font: inherit;
  text-align: left;
}

.history-row-icon,
.history-empty-icon {
  display: grid;
  place-items: center;
  border-radius: 13px;
  background: #eef0ff;
  color: rgb(var(--primary-color));
}

.history-row-icon {
  width: 42px;
  height: 42px;
  font-size: 22px;
}

.history-row-copy {
  min-width: 0;
}

.history-row-title {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 16px;
}

.history-row-title h2 {
  min-width: 0;
  margin: 0;
  overflow: hidden;
  color: #343845;
  font-size: 15px;
  line-height: 1.3;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.history-row-title time {
  flex: 0 0 auto;
  color: #9399a7;
  font-size: 11px;
}

.history-row-copy p {
  display: -webkit-box;
  margin: 5px 0 8px;
  overflow: hidden;
  color: #737b8b;
  font-size: 12px;
  line-height: 1.55;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 1;
}

.history-row-meta {
  display: flex;
  gap: 8px;
}

.history-row-meta span {
  padding: 3px 7px;
  border-radius: 999px;
  background: #f2f4f8;
  color: #737b8b;
  font-size: 10px;
  font-weight: 650;
}

.history-row-meta span:last-child {
  background: #edf7ef;
  color: #4f805b;
}

.history-row-arrow {
  color: #9fa6b5;
  font-size: 18px;
}

.history-remove {
  position: absolute;
  top: 12px;
  right: 13px;
  padding: 3px;
  border: 0;
  background: transparent;
  color: #a0a6b3;
  cursor: pointer;
  font: inherit;
  font-size: 11px;
}

.history-remove:hover {
  color: #6f7788;
}

.history-empty {
  display: flex;
  min-height: 0;
  flex: 1;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 44px 28px;
  text-align: center;
}

.history-empty-icon {
  width: 50px;
  height: 50px;
  font-size: 25px;
}

.history-empty h2 {
  margin: 16px 0 6px;
  color: #3a3e4b;
  font-size: 17px;
}

.history-empty p {
  margin: 0 0 18px;
  color: #8b92a1;
  font-size: 12px;
}

@media (max-width: 720px) {
  .history-workspace {
    padding: 0 !important;
  }

  .history-panel {
    border: 0;
    border-radius: 0;
    box-shadow: none;
  }

  .history-heading {
    padding: 22px 20px 17px;
  }

  .history-list {
    padding: 12px;
  }

  .history-open {
    padding: 14px 56px 14px 14px;
  }

  .history-row-title {
    display: block;
  }

  .history-row-title time {
    display: block;
    margin-top: 3px;
  }
}
</style>
