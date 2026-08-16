import { computed, reactive, ref } from 'vue';
import { useRoute } from 'vue-router';
import { defineStore } from 'pinia';
import { useLoading } from '@sa/hooks';
import {
  LOCAL_SESSION_TOKEN,
  currentLocalAccount,
  loginLocalAccount,
  logoutLocalAccount,
  type LocalAuthStatus
} from '@/service/local-auth';
import { useRouterPush } from '@/hooks/common/router';
import { SetupStoreId } from '@/enum';
import { $t } from '@/locales';
import { useRouteStore } from '../route';
import { clearAuthStorage, getToken } from './shared';

export const useAuthStore = defineStore(SetupStoreId.Auth, () => {
  const route = useRoute();
  const routeStore = useRouteStore();
  const { toLogin, redirectFromLogin } = useRouterPush(false);
  const { loading: loginLoading, startLoading, endLoading } = useLoading();

  const token = ref(getToken());
  const defaultUserInfo: Api.Auth.UserInfo = {
    id: 0,
    username: '',
    role: 'USER',
    orgTags: [],
    primaryOrg: ''
  };

  const userInfo: Api.Auth.UserInfo = reactive({ ...defaultUserInfo });

  const isAdmin = computed(() => userInfo.role === 'ADMIN');

  /** is super role in static route */
  const isStaticSuper = computed(() => {
    const { VITE_AUTH_ROUTE_MODE, VITE_STATIC_SUPER_ROLE } = import.meta.env;

    return VITE_AUTH_ROUTE_MODE === 'static' && userInfo.role === VITE_STATIC_SUPER_ROLE;
  });

  /** Is login */
  const isLogin = computed(() => Boolean(token.value));

  function applyLocalUser(status: LocalAuthStatus) {
    let stableId = 0;
    for (const character of status.username) stableId = (stableId * 31 + character.codePointAt(0)!) >>> 0;
    Object.assign(userInfo, {
      id: stableId || 1,
      username: status.username,
      role: 'USER' as const,
      orgTags: [],
      primaryOrg: ''
    });
  }

  /** Reset auth store */
  async function resetStore() {
    clearAuthStorage();
    token.value = '';
    Object.assign(userInfo, defaultUserInfo);

    if (!route.meta.constant) {
      await toLogin();
    }

    routeStore.resetStore();
  }

  /**
   * Login
   *
   * @param userName User name
   * @param password Password
   * @param [redirect=true] Whether to redirect after login. Default is `true`
   */
  async function login(userName: string, password: string, redirect = true) {
    startLoading();
    try {
      const status = await loginLocalAccount(userName, password);
      localStg.set('token', LOCAL_SESSION_TOKEN);
      token.value = LOCAL_SESSION_TOKEN;
      applyLocalUser(status);
      await redirectFromLogin(redirect);
      window.$notification?.success({
        title: $t('page.login.common.loginSuccess'),
        content: $t('page.login.common.welcomeBack', { userName: userInfo.username }),
        duration: 4500
      });
      return true;
    } catch {
      await resetStore();
      window.$message?.error('用户名或密码错误');
      return false;
    } finally {
      endLoading();
    }
  }

  async function getUserInfo() {
    try {
      const status = await currentLocalAccount();
      applyLocalUser(status);
      return true;
    } catch {
      return false;
    }
  }

  async function initUserInfo() {
    const hasToken = getToken();

    if (hasToken) {
      const pass = await getUserInfo();

      if (!pass) {
        resetStore();
      }
    }
  }

  /** Set token (used for seamless token refresh) */
  function setToken(newToken: string) {
    token.value = newToken;
    localStg.set('token', newToken);
  }

  async function logout() {
    try {
      await logoutLocalAccount();
    } finally {
      await resetStore();
    }
  }

  return {
    token,
    userInfo,
    isStaticSuper,
    isLogin,
    isAdmin,
    loginLoading,
    resetStore,
    login,
    logout,
    initUserInfo,
    setToken
  };
});
