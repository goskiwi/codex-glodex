import 'vue-markdown-shiki/style';
import markdownPlugin from '@/vendor/vue-markdown-shiki';
import './plugins/assets';
import { setupAppVersionNotification, setupDayjs, setupIconifyOffline, setupLoading, setupNProgress } from './plugins';
import { setupStore } from './store';
import { setupRouter } from './router';
import { setupI18n } from './locales';
import App from './App.vue';

const preloadRecoveryKey = 'glodex-preload-recovery';
window.addEventListener('vite:preloadError', event => {
  event.preventDefault();
  if (sessionStorage.getItem(preloadRecoveryKey) === BUILD_TIME) return;
  sessionStorage.setItem(preloadRecoveryKey, BUILD_TIME);
  window.location.reload();
});

async function setupApp() {
  setupLoading();

  setupNProgress();

  setupIconifyOffline();

  setupDayjs();

  const app = createApp(App);

  setupStore(app);

  await setupRouter(app);

  setupI18n(app);

  setupAppVersionNotification();

  app.use(markdownPlugin);

  app.mount('#app');
}

setupApp();
