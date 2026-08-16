import { computed, ref } from 'vue';
import { defineStore } from 'pinia';
import type { ConversationThread } from '@/service/memory';
import { deleteConversationThread, listConversationThreads, listConversationTurns } from '@/service/memory';
import { SetupStoreId } from '@/enum';

/** Authenticated conversation history backed only by the durable service. */
export const useResearchHistoryStore = defineStore(SetupStoreId.ResearchHistory, () => {
  const threads = ref<ConversationThread[]>([]);
  const pendingThreadId = ref<string | null>(null);
  const loading = ref(false);
  const count = computed(() => threads.value.length);

  async function load() {
    loading.value = true;
    try {
      threads.value = (await listConversationThreads()).threads;
    } finally {
      loading.value = false;
    }
  }

  async function remove(threadId: string) {
    await deleteConversationThread(threadId);
    threads.value = threads.value.filter(item => item.threadId !== threadId);
    if (pendingThreadId.value === threadId) pendingThreadId.value = null;
  }

  function open(threadId: string) {
    if (!threads.value.some(item => item.threadId === threadId)) return;
    pendingThreadId.value = threadId;
  }

  function consumePendingThreadId() {
    const threadId = pendingThreadId.value;
    pendingThreadId.value = null;
    return threadId;
  }

  async function turns(threadId: string) {
    return (await listConversationTurns(threadId)).turns;
  }

  return { threads, pendingThreadId, loading, count, load, remove, open, consumePendingThreadId, turns };
});
