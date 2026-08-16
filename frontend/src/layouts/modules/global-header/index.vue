<script setup lang="ts">
import { useFullscreen } from '@vueuse/core';
import { useAppStore } from '@/store/modules/app';
import { useThemeStore } from '@/store/modules/theme';
import GlobalSearch from '../global-search/index.vue';
import UserAvatar from './components/user-avatar.vue';

defineOptions({
  name: 'GlobalHeader'
});

interface Props {
  /** Whether to show the logo */
  // showLogo?: App.Global.HeaderProps['showLogo'];
  /** Whether to show the menu toggler */
  showMenuToggler?: App.Global.HeaderProps['showMenuToggler'];
  /** Whether to show the menu */
  // showMenu?: App.Global.HeaderProps['showMenu'];
}

defineProps<Props>();

const appStore = useAppStore();
const themeStore = useThemeStore();
const { isFullscreen, toggle } = useFullscreen();

</script>

<template>
  <DarkModeContainer class="ml-12 h-full flex-y-center justify-between bg-transparent">
    <div id="header-extra" class="h-full flex-col justify-center"></div>
    <MenuToggler
      v-if="showMenuToggler && appStore.isMobile"
      :collapsed="appStore.siderCollapse"
      @click="appStore.toggleSiderCollapse"
    />
    <div
      class="global-header-tools mr-5 h-11 flex-y-center justify-end rounded-full bg-white px-4 shadow-[0_10px_28px_rgba(67,78,107,0.1)] ring-1 ring-[#ffffffcc] dark:bg-[#242424] dark:shadow-[0_10px_28px_rgba(0,0,0,0.22)] dark:ring-[#ffffff0d]"
    >
      <GlobalSearch />
      <FullScreen v-if="!appStore.isMobile" :full="isFullscreen" @click="toggle" />
      <LangSwitch
        v-if="themeStore.header.multilingual.visible"
        class="header-lang-switch"
        :lang="appStore.locale"
        :lang-options="appStore.localeOptions"
        @change-lang="appStore.changeLocale"
      />
      <ThemeSchemaSwitch
        class="header-theme-schema"
        :theme-schema="themeStore.themeScheme"
        :is-dark="themeStore.darkMode"
        @switch="themeStore.toggleThemeScheme"
      />
      <UserAvatar />
    </div>
  </DarkModeContainer>
</template>

<style scoped>
@media (max-width: 760px) {
  .global-header-tools {
    height: 40px;
    margin-right: 8px;
    padding: 0 4px;
  }

  :deep(.header-lang-switch),
  :deep(.header-theme-schema) {
    display: none;
  }
}
</style>
