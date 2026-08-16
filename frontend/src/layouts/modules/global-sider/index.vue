<script setup lang="ts">
import { computed } from 'vue';
import { GLOBAL_SIDER_MENU_ID } from '@/constants/app';
import { useAppStore } from '@/store/modules/app';
import { useThemeStore } from '@/store/modules/theme';
import GlobalLogo from '../global-logo/index.vue';

defineOptions({
  name: 'GlobalSider'
});

const appStore = useAppStore();
const themeStore = useThemeStore();

const darkMenu = computed(() => !themeStore.darkMode && themeStore.sider.inverted);
</script>

<template>
  <DarkModeContainer class="size-full flex-col-stretch shadow-sider" :inverted="darkMenu">
    <GlobalLogo :show-title="!appStore.siderCollapse" :style="{ height: themeStore.header.height + 'px' }" />
    <div :id="GLOBAL_SIDER_MENU_ID" class="flex-1-hidden"></div>
  </DarkModeContainer>
</template>

<style scoped></style>
