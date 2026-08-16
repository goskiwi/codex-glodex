import { computed, ref } from 'vue';
import { defineStore } from 'pinia';
import type { MemoryCategory, MemoryEntry } from '@/service/memory';
import { createMemory, deleteMemory, listMemory } from '@/service/memory';
import { SetupStoreId } from '@/enum';

/** Authenticated, server-backed long-term memory. Browser storage is not a source of truth. */
export const usePreferenceStore = defineStore(SetupStoreId.Preferences, () => {
  const activeEntries = ref<MemoryEntry[]>([]);
  const loading = ref(false);
  const count = computed(() => activeEntries.value.length);

  async function load() {
    loading.value = true;
    try {
      const result = await listMemory();
      activeEntries.value = result.activeEntries;
    } finally {
      loading.value = false;
    }
  }

  async function add(category: Exclude<MemoryCategory, 'history'>, content: string) {
    const entry = await createMemory(category, content.trim());
    activeEntries.value = [entry, ...activeEntries.value.filter(item => item.entryId !== entry.entryId)];
    return entry;
  }

  async function remove(entryId: string) {
    await deleteMemory(entryId);
    activeEntries.value = activeEntries.value.filter(item => item.entryId !== entryId);
  }

  return { activeEntries, loading, count, load, add, remove };
});
