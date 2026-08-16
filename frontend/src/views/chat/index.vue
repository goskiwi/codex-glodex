<script setup lang="ts">
import { computed } from 'vue';
import { useRoute } from 'vue-router';
import ReferencePreviewPage from './modules/reference-preview-page.vue';
import ResearchWorkspace from './modules/research-workspace.vue';

defineOptions({
  name: 'chat'
});

const route = useRoute();
const showReferencePreview = computed(() => route.query.preview === 'reference');
</script>

<template>
  <div v-if="showReferencePreview" class="h-full">
    <ReferencePreviewPage />
  </div>
  <div v-else class="h-full bg-layout p-3 pt-4">
    <ResearchWorkspace />
  </div>
</template>

<style scoped></style>
