<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import type { MemoryEntry } from '@/service/memory';
import { usePreferenceStore } from '@/store/modules/preferences';

defineOptions({ name: 'KnowledgeBase' });

type EditableCategory = 'preference' | 'blacklist';
type BlacklistField = 'material' | 'brand' | 'platform' | 'item_id';

const preferenceStore = usePreferenceStore();
const category = ref<EditableCategory>('preference');
const preferenceContent = ref('');
const blacklistField = ref<BlacklistField>('material');
const blacklistValue = ref('');
const submitting = ref(false);

const blacklistFields: Array<{ value: BlacklistField; label: string; placeholder: string }> = [
  { value: 'material', label: '材质', placeholder: '例如：塑料' },
  { value: 'brand', label: '品牌', placeholder: '例如：Acme' },
  { value: 'platform', label: '平台', placeholder: 'amazon / shopee / aliexpress' },
  { value: 'item_id', label: '商品 ID', placeholder: '输入商品的准确 ID' }
];

const selectedBlacklistField = computed(
  () => blacklistFields.find(item => item.value === blacklistField.value) ?? blacklistFields[0]!
);

function entryLabel(entry: MemoryEntry) {
  if (entry.category === 'preference') return '偏好';
  if (entry.category === 'history') return '历史';
  const [field] = entry.content.split(':', 1);
  return blacklistFields.find(item => item.value === field)?.label ?? '排除规则';
}

function originLabel(entry: MemoryEntry) {
  if (entry.origin === 'manual') return '手动添加';
  return '对话中确认';
}

async function savePreference() {
  const content =
    category.value === 'preference'
      ? preferenceContent.value.trim()
      : `${blacklistField.value}:${blacklistValue.value.trim()}`;
  if (!content || (category.value === 'blacklist' && !blacklistValue.value.trim())) {
    window.$message?.warning(category.value === 'preference' ? '先写下一条偏好。' : '请输入要排除的准确值。');
    return;
  }

  submitting.value = true;
  try {
    await preferenceStore.add(category.value, content);
    preferenceContent.value = '';
    blacklistValue.value = '';
    window.$message?.success(category.value === 'preference' ? '偏好已保存。' : '排除规则已生效。');
  } catch (error) {
    window.$message?.error(error instanceof Error ? error.message : '保存失败。');
  } finally {
    submitting.value = false;
  }
}

async function removeEntry(entry: MemoryEntry) {
  try {
    await preferenceStore.remove(entry.entryId);
    window.$message?.success('已删除。');
  } catch (error) {
    window.$message?.error(error instanceof Error ? error.message : '删除失败。');
  }
}

onMounted(async () => {
  try {
    await preferenceStore.load();
  } catch (error) {
    window.$message?.error(error instanceof Error ? error.message : '无法读取长期记忆。');
  }
});
</script>

<template>
  <main class="preference-workspace" aria-label="购物偏好库">
    <section class="preference-panel">
      <header class="preference-heading">
        <div>
          <p>长期记忆</p>
          <h1>购物偏好与排除规则</h1>
          <span>这里只展示服务端已保存、后续研究真正会读取的内容。</span>
        </div>
        <span class="preference-count">{{ preferenceStore.count }} 条有效记忆</span>
      </header>

      <section class="preference-tools" aria-label="长期记忆编辑">
        <form class="preference-editor" @submit.prevent="savePreference">
          <div class="tool-heading">
            <div>
              <h2>新增长期记忆</h2>
              <p>偏好用于排序与说明；排除规则会直接过滤匹配商品。</p>
            </div>
            <icon-material-symbols:bookmark-add-rounded aria-hidden="true" />
          </div>

          <div class="memory-kind-switch" role="group" aria-label="记忆类型">
            <button type="button" :class="{ active: category === 'preference' }" @click="category = 'preference'">
              购物偏好
            </button>
            <button type="button" :class="{ active: category === 'blacklist' }" @click="category = 'blacklist'">
              排除规则
            </button>
          </div>

          <template v-if="category === 'preference'">
            <label class="field-label" for="preference-content">偏好内容</label>
            <textarea
              id="preference-content"
              v-model="preferenceContent"
              class="preference-textarea"
              maxlength="512"
              placeholder="例如：预算有限时优先性价比，配送希望 7 天内到达。"
            />
            <div class="field-meta">
              <span>由后端做语义相关度筛选</span>
              <span>{{ preferenceContent.length }} / 512</span>
            </div>
          </template>

          <template v-else>
            <label class="field-label" for="blacklist-field">排除字段</label>
            <select id="blacklist-field" v-model="blacklistField" class="preference-input">
              <option v-for="field in blacklistFields" :key="field.value" :value="field.value">
                {{ field.label }}
              </option>
            </select>
            <label class="field-label" for="blacklist-value">准确值</label>
            <input
              id="blacklist-value"
              v-model="blacklistValue"
              class="preference-input"
              maxlength="128"
              :placeholder="selectedBlacklistField.placeholder"
            />
            <p class="field-help">保存为 {{ blacklistField }}:值；只有可验证的商品事实才会被过滤。</p>
          </template>

          <button class="preference-submit" type="submit" :disabled="submitting">
            {{ submitting ? '正在保存' : '保存到长期记忆' }}
            <icon-material-symbols:arrow-forward-rounded aria-hidden="true" />
          </button>
        </form>

        <section class="preference-tester" aria-label="生效说明">
          <div class="tool-heading">
            <div>
              <h2>实际生效方式</h2>
              <p>不在浏览器里模拟匹配，研究请求会读取后端的真实结果。</p>
            </div>
            <icon-material-symbols:search-insights-rounded aria-hidden="true" />
          </div>
          <div class="test-placeholder memory-explanation">
            <icon-material-symbols:tips-and-updates-rounded aria-hidden="true" />
            <p>偏好只有达到相关度门槛才会带入当前研究，最多 5 条。</p>
          </div>
          <div class="test-placeholder memory-explanation">
            <icon-material-symbols:check-circle-rounded aria-hidden="true" />
            <p>排除规则按材质、品牌、平台或商品 ID 精确匹配，不猜测。</p>
          </div>
        </section>
      </section>

      <section class="saved-preferences" aria-label="已保存的偏好">
        <div class="saved-heading">
          <div>
            <h2>当前有效记忆</h2>
            <p>来自手动添加或对话中的明确长期表达。</p>
          </div>
          <span>{{ preferenceStore.count }} 条</span>
        </div>

        <div v-if="preferenceStore.count" class="preference-list">
          <article
            v-for="preference in preferenceStore.activeEntries"
            :key="preference.entryId"
            class="preference-item"
          >
            <div class="preference-item-icon" aria-hidden="true">
              <icon-material-symbols:favorite-rounded />
            </div>
            <div class="preference-item-copy">
              <p>{{ preference.content }}</p>
              <div class="preference-item-meta">
                <span class="preference-tag">{{ entryLabel(preference) }}</span>
                <span>{{ originLabel(preference) }}</span>
              </div>
            </div>
            <button
              class="preference-remove"
              type="button"
              :aria-label="`删除偏好：${preference.content}`"
              @click="removeEntry(preference)"
            >
              删除
            </button>
          </article>
        </div>

        <div v-else class="preference-empty">
          <div class="preference-empty-icon">
            <icon-material-symbols:favorite-outline-rounded aria-hidden="true" />
          </div>
          <h2>{{ preferenceStore.loading ? '正在读取长期记忆' : '还没有长期记忆' }}</h2>
          <p>可以手动添加，也可以在研究对话里明确说“以后都……”或“我长期偏好……”。</p>
        </div>
      </section>
    </section>
  </main>
</template>

<style scoped>
.preference-workspace {
  display: flex;
  height: 100%;
  min-height: 0;
  padding: 20px 28px 28px !important;
  overflow: hidden;
  background: #f5f8fc;
  color: #303541;
}

.preference-panel {
  width: min(1010px, 100%);
  min-height: 0;
  flex: 1;
  margin: 0 auto;
  overflow-y: auto;
  border: 1px solid #e4e8ef;
  border-radius: 20px;
  background: #fff;
  box-shadow: 0 14px 34px rgb(76 88 118 / 0.08);
}

.preference-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  padding: 22px 28px 16px;
  border-bottom: 1px solid #e9edf3;
}

.preference-heading p,
.saved-heading p,
.tool-heading p {
  margin: 0;
}

.preference-heading > div > p {
  margin-bottom: 6px;
  color: rgb(var(--primary-color));
  font-size: 12px;
  font-weight: 700;
}

.preference-heading h1 {
  margin: 0;
  color: #303440;
  font-size: 23px;
  letter-spacing: -0.035em;
  line-height: 1.2;
}

.preference-heading > div > span {
  display: block;
  margin-top: 7px;
  color: #8a91a0;
  font-size: 12px;
}

.preference-count,
.saved-heading > span {
  flex: 0 0 auto;
  padding: 6px 10px;
  border-radius: 999px;
  background: #f1f2ff;
  color: rgb(var(--primary-color));
  font-size: 12px;
  font-weight: 700;
}

.preference-tools {
  display: grid;
  grid-template-columns: minmax(0, 1.08fr) minmax(300px, 0.92fr);
  gap: 14px;
  padding: 14px 28px;
  border-bottom: 1px solid #edf0f4;
}

.preference-editor,
.preference-tester {
  min-width: 0;
  border: 1px solid #e8ebf1;
  border-radius: 15px;
  background: #fafbfe;
  padding: 14px;
}

.tool-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.tool-heading h2,
.saved-heading h2,
.preference-empty h2 {
  margin: 0;
  color: #363a47;
  font-size: 15px;
  line-height: 1.35;
}

.tool-heading p,
.saved-heading p {
  margin-top: 4px;
  color: #8a91a0;
  font-size: 11px;
  line-height: 1.45;
}

.tool-heading > svg {
  flex: 0 0 auto;
  color: rgb(var(--primary-color));
  font-size: 21px;
}

.memory-kind-switch {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px;
  margin-bottom: 12px;
  padding: 3px;
  border-radius: 10px;
  background: #edf0f5;
}

.memory-kind-switch button {
  padding: 7px 10px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: #737b8b;
  cursor: pointer;
  font: inherit;
  font-size: 11px;
  font-weight: 700;
}

.memory-kind-switch button.active {
  background: #fff;
  color: rgb(var(--primary-color));
  box-shadow: 0 2px 7px rgb(69 78 109 / 0.08);
}

.field-label {
  display: block;
  margin: 10px 0 6px;
  color: #5c6473;
  font-size: 12px;
  font-weight: 700;
}

.field-label:first-of-type {
  margin-top: 0;
}

.preference-textarea,
.preference-input,
.test-input {
  box-sizing: border-box;
  width: 100%;
  border: 1px solid #dce1ea;
  border-radius: 10px;
  outline: 0;
  background: #fff;
  color: #363a47;
  font: inherit;
  font-size: 12px;
  transition:
    border-color 160ms ease,
    box-shadow 160ms ease;
}

.preference-textarea::placeholder,
.preference-input::placeholder,
.test-input::placeholder {
  color: #a3a9b5;
}

.preference-textarea:focus,
.preference-input:focus,
.test-input:focus {
  border-color: rgb(var(--primary-color) / 0.7);
  box-shadow: 0 0 0 3px rgb(var(--primary-color) / 0.1);
}

.preference-textarea {
  min-height: 74px;
  resize: vertical;
  padding: 10px 11px;
  line-height: 1.6;
}

.preference-input {
  height: 36px;
  padding: 0 11px;
}

select.preference-input {
  cursor: pointer;
}

.field-meta {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  margin-top: 5px;
  color: #9aa1af;
  font-size: 10px;
}

.field-help {
  margin: 5px 0 0;
  color: #9aa1af;
  font-size: 10px;
}

.preference-submit {
  display: inline-flex;
  width: 100%;
  height: 36px;
  align-items: center;
  justify-content: center;
  gap: 6px;
  margin-top: 12px;
  border: 1px solid rgb(var(--primary-color));
  border-radius: 10px;
  background: rgb(var(--primary-color));
  color: #fff;
  cursor: pointer;
  font: inherit;
  font-size: 12px;
  font-weight: 700;
  transition:
    background-color 160ms ease,
    transform 160ms ease;
}

.preference-submit:hover {
  background: rgb(var(--primary-color) / 0.9);
}

.preference-submit:active {
  transform: translateY(1px);
}

.preference-submit:disabled {
  cursor: wait;
  opacity: 0.65;
}

.memory-explanation + .memory-explanation {
  margin-top: 8px;
}

.preference-submit svg {
  font-size: 17px;
}

.test-input-shell {
  display: flex;
  align-items: center;
  border: 1px solid #dce1ea;
  border-radius: 10px;
  background: #fff;
  transition:
    border-color 160ms ease,
    box-shadow 160ms ease;
}

.test-input-shell:focus-within {
  border-color: rgb(var(--primary-color) / 0.7);
  box-shadow: 0 0 0 3px rgb(var(--primary-color) / 0.1);
}

.test-input-shell > svg {
  flex: 0 0 auto;
  margin-left: 11px;
  color: #98a0ae;
  font-size: 18px;
}

.test-input {
  height: 36px;
  min-width: 0;
  border: 0;
  box-shadow: none !important;
  padding: 0 10px 0 8px;
}

.test-result,
.test-placeholder {
  min-height: 140px;
  margin-top: 15px;
  border-top: 1px solid #e7ebf1;
  padding-top: 14px;
}

.test-result-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
}

.test-result-heading strong {
  color: #3c4150;
  font-size: 12px;
}

.test-result-heading span {
  color: #9aa1af;
  font-size: 10px;
}

.test-match-list {
  display: grid;
  gap: 9px;
  margin: 11px 0 0;
  padding: 0;
  list-style: none;
}

.test-match-list li {
  display: grid;
  grid-template-columns: 16px minmax(0, 1fr);
  align-items: flex-start;
  gap: 7px;
}

.test-match-list li > svg {
  margin-top: 1px;
  color: #5a9466;
  font-size: 16px;
}

.test-match-list p {
  display: -webkit-box;
  margin: 0;
  overflow: hidden;
  color: #555d6d;
  font-size: 11px;
  line-height: 1.45;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 1;
}

.test-match-list span {
  display: block;
  margin-top: 2px;
  color: #949ba9;
  font-size: 10px;
}

.test-placeholder,
.test-no-match {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #98a0ae;
  font-size: 11px;
}

.test-placeholder {
  justify-content: center;
  text-align: center;
}

.test-placeholder svg,
.test-no-match svg {
  flex: 0 0 auto;
  color: #a6acf0;
  font-size: 19px;
}

.test-placeholder p,
.test-no-match p {
  margin: 0;
  line-height: 1.5;
}

.saved-preferences {
  padding: 20px 28px 28px;
}

.saved-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;
}

.preference-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.preference-item {
  display: grid;
  grid-template-columns: 30px minmax(0, 1fr) auto;
  align-items: flex-start;
  gap: 10px;
  min-height: 90px;
  padding: 14px;
  border: 1px solid #e9edf3;
  border-radius: 14px;
  background: #fff;
  transition:
    border-color 160ms ease,
    box-shadow 160ms ease,
    transform 160ms ease;
}

.preference-item:hover {
  border-color: rgb(var(--primary-color) / 0.3);
  box-shadow: 0 8px 18px rgb(80 88 157 / 0.06);
  transform: translateY(-1px);
}

.preference-item-icon,
.preference-empty-icon {
  display: grid;
  place-items: center;
  border-radius: 10px;
  background: #f0f1ff;
  color: rgb(var(--primary-color));
}

.preference-item-icon {
  width: 30px;
  height: 30px;
  font-size: 16px;
}

.preference-item-copy {
  min-width: 0;
}

.preference-item-copy > p {
  display: -webkit-box;
  margin: 0;
  overflow: hidden;
  color: #4a5060;
  font-size: 12px;
  line-height: 1.55;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.preference-item-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 9px;
}

.preference-tag {
  padding: 3px 6px;
  border-radius: 999px;
  background: #f1f2f5;
  color: #717989;
  font-size: 10px;
  font-weight: 650;
}

.preference-item-meta time {
  margin-left: auto;
  color: #a1a7b3;
  font-size: 10px;
  white-space: nowrap;
}

.preference-remove {
  padding: 2px 0 2px 5px;
  border: 0;
  background: transparent;
  color: #a0a6b2;
  cursor: pointer;
  font: inherit;
  font-size: 10px;
}

.preference-remove:hover {
  color: #737b8b;
}

.preference-empty {
  display: flex;
  min-height: 180px;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 28px;
  border: 1px dashed #dce1ea;
  border-radius: 14px;
  text-align: center;
}

.preference-empty-icon {
  width: 44px;
  height: 44px;
  font-size: 22px;
}

.preference-empty h2 {
  margin-top: 12px;
}

.preference-empty p {
  margin: 5px 0 0;
  color: #8b92a1;
  font-size: 11px;
}

@media (max-width: 920px) {
  .preference-tools {
    grid-template-columns: 1fr;
  }

  .test-result,
  .test-placeholder {
    min-height: auto;
  }
}

@media (max-width: 720px) {
  .preference-workspace {
    padding: 0 !important;
  }

  .preference-panel {
    border: 0;
    border-radius: 0;
    box-shadow: none;
  }

  .preference-heading,
  .preference-tools,
  .saved-preferences {
    padding-right: 20px;
    padding-left: 20px;
  }

  .preference-heading {
    padding-top: 22px;
    padding-bottom: 17px;
  }

  .preference-list {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 480px) {
  .preference-heading {
    display: block;
  }

  .preference-count {
    display: inline-block;
    margin-top: 12px;
  }

  .preference-editor,
  .preference-tester {
    padding: 15px;
  }
}
</style>
