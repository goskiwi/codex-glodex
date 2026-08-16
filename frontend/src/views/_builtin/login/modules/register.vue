<script setup lang="ts">
import { reactive, ref } from 'vue';
import { registerLocalAccount } from '@/service/local-auth';
import { $t } from '@/locales';

defineOptions({ name: 'Register' });

const { toggleLoginModule } = useRouterPush();
const { formRef, validate } = useNaiveForm();
const loading = ref(false);

const model = reactive({
  username: '',
  password: '',
  confirmPassword: ''
});

const rules = computed(() => {
  const { formRules, createConfirmPwdRule } = useFormRules();
  return {
    username: formRules.userName,
    password: formRules.pwd,
    confirmPassword: createConfirmPwdRule(model.password)
  };
});

async function handleSubmit() {
  await validate();
  loading.value = true;
  try {
    await registerLocalAccount(model.username, model.password);
    window.$message?.success('本地账户注册成功，请登录');
    toggleLoginModule('pwd-login');
  } catch {
    window.$message?.error('注册失败：用户名可能已存在');
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <NForm
    ref="formRef"
    :model="model"
    :rules="rules"
    size="large"
    :show-label="false"
    class="mx-auto max-w-420px"
    @keyup.enter="handleSubmit"
  >
    <div class="mb-6">
      <div class="text-22px font-700">创建本地账户</div>
      <div class="mt-2 text-13px color-#888">账户只用于本机 Durable Agent，不接入外部用户系统。</div>
    </div>
    <NFormItem path="username">
      <NInput v-model:value="model.username" :placeholder="$t('page.login.common.userNamePlaceholder')">
        <template #prefix><icon-ant-design:user-outlined /></template>
      </NInput>
    </NFormItem>
    <NFormItem path="password">
      <NInput
        v-model:value="model.password"
        type="password"
        show-password-on="click"
        :placeholder="$t('page.login.common.passwordPlaceholder')"
      >
        <template #prefix><icon-ant-design:key-outlined /></template>
      </NInput>
    </NFormItem>
    <NFormItem path="confirmPassword">
      <NInput
        v-model:value="model.confirmPassword"
        type="password"
        show-password-on="click"
        :placeholder="$t('page.login.common.confirmPasswordPlaceholder')"
      >
        <template #prefix><icon-ant-design:key-outlined /></template>
      </NInput>
    </NFormItem>
    <NSpace vertical :size="18" class="w-full">
      <NButton type="primary" size="large" round block :loading="loading" @click="handleSubmit">
        {{ $t('page.login.common.register') }}
      </NButton>
      <NButton block @click="toggleLoginModule('pwd-login')">{{ $t('page.login.common.back') }}</NButton>
    </NSpace>
  </NForm>
</template>
