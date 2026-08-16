<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useCartStore } from '@/store/modules/cart';
import { useRouterPush } from '@/hooks/common/router';

defineOptions({
  name: 'ShoppingCart'
});

type CartTab = 'cart' | 'favorites';

const { routerPushByKey } = useRouterPush();
const cartStore = useCartStore();
const activeTab = ref<CartTab>('cart');
const selectedCartIds = ref<string[]>([]);
const selectedFavoriteIds = ref<string[]>([]);

const activeItems = computed(() => (activeTab.value === 'cart' ? cartStore.items : cartStore.favoriteItems));
const activeSelectedIds = computed({
  get: () => (activeTab.value === 'cart' ? selectedCartIds.value : selectedFavoriteIds.value),
  set: ids => {
    if (activeTab.value === 'cart') {
      selectedCartIds.value = ids;
      return;
    }

    selectedFavoriteIds.value = ids;
  }
});
const selectedItems = computed(() => activeItems.value.filter(item => activeSelectedIds.value.includes(item.id)));
const selectedCount = computed(() => selectedItems.value.length);
const allItemsSelected = computed(
  () => activeItems.value.length > 0 && activeItems.value.every(item => activeSelectedIds.value.includes(item.id))
);

function syncSelectedIds(selectedIds: string[], itemIds: string[]) {
  const availableIds = new Set(itemIds);
  const retainedIds = selectedIds.filter(id => availableIds.has(id));
  const addedIds = itemIds.filter(id => !retainedIds.includes(id));

  return [...retainedIds, ...addedIds];
}

watch(
  () => cartStore.items.map(item => item.id),
  itemIds => {
    selectedCartIds.value = syncSelectedIds(selectedCartIds.value, itemIds);
  },
  { immediate: true }
);

watch(
  () => cartStore.favoriteItems.map(item => item.id),
  itemIds => {
    selectedFavoriteIds.value = syncSelectedIds(selectedFavoriteIds.value, itemIds);
  },
  { immediate: true }
);

function switchTab(tab: CartTab) {
  activeTab.value = tab;
}

function isSelected(itemId: string) {
  return activeSelectedIds.value.includes(itemId);
}

function toggleSelection(itemId: string) {
  if (isSelected(itemId)) {
    activeSelectedIds.value = activeSelectedIds.value.filter(id => id !== itemId);
    return;
  }

  activeSelectedIds.value = [...activeSelectedIds.value, itemId];
}

function toggleSelectAll() {
  activeSelectedIds.value = allItemsSelected.value ? [] : activeItems.value.map(item => item.id);
}

function removeItem(itemId: string) {
  if (activeTab.value === 'cart') {
    cartStore.remove(itemId);
    return;
  }

  cartStore.removeFavorite(itemId);
}

function addFavoritesToCart() {
  if (!selectedItems.value.length) return;

  selectedItems.value.forEach(({ addedAt: _addedAt, ...item }) => cartStore.add(item));
  selectedCartIds.value = selectedItems.value.map(item => item.id);
  selectedFavoriteIds.value = [];
  activeTab.value = 'cart';
  window.$message?.success(`已将 ${selectedItems.value.length} 件商品加入本地清单。`);
}
</script>

<template>
  <div class="cart-route-shell">
    <main class="cart-stage" aria-label="商品清单与收藏">
      <aside class="cart-panel" aria-label="商品清单侧栏">
        <header class="cart-panel-header">
          <div>
            <h2>商品清单与收藏</h2>
            <p>保存当前商品检索服务实际返回的商品</p>
          </div>
        </header>

        <div class="cart-tabs" role="tablist" aria-label="商品清单内容">
          <button
            type="button"
            role="tab"
            :aria-selected="activeTab === 'cart'"
            :class="{ active: activeTab === 'cart' }"
            @click="switchTab('cart')"
          >
            本地清单
            <span>{{ cartStore.count }}</span>
          </button>
          <button
            type="button"
            role="tab"
            :aria-selected="activeTab === 'favorites'"
            :class="{ active: activeTab === 'favorites' }"
            @click="switchTab('favorites')"
          >
            收藏
            <span>{{ cartStore.favoriteCount }}</span>
          </button>
        </div>

        <section
          v-if="activeItems.length"
          class="cart-item-list"
          :aria-label="activeTab === 'cart' ? '本地清单商品' : '收藏商品'"
        >
          <article
            v-for="item in activeItems"
            :key="item.id"
            class="cart-item"
            :class="{ 'is-selected': isSelected(item.id) }"
          >
            <button
              class="cart-checkbox"
              type="button"
              :aria-label="isSelected(item.id) ? `取消选择${item.title}` : `选择${item.title}`"
              :aria-pressed="isSelected(item.id)"
              @click="toggleSelection(item.id)"
            >
              <icon-material-symbols:check-rounded v-if="isSelected(item.id)" />
            </button>
            <div class="is-generic cart-item-image" role="img" :aria-label="item.title">
              <icon-material-symbols:inventory-2-outline-rounded aria-hidden="true" />
            </div>
            <div class="cart-item-copy">
              <span>{{ item.category }}</span>
              <h3>{{ item.title }}</h3>
              <strong>{{ item.price || '价格待确认' }}</strong>
            </div>
            <button class="cart-remove" type="button" :aria-label="`移除${item.title}`" @click="removeItem(item.id)">
              移除
            </button>
          </article>
        </section>

        <section v-else class="cart-empty">
          <div class="cart-empty-icon">
            <icon-material-symbols:shopping-bag-outline v-if="activeTab === 'cart'" />
            <icon-material-symbols:favorite-outline-rounded v-else />
          </div>
          <h3>{{ activeTab === 'cart' ? '本地清单还是空的' : '还没有收藏的商品' }}</h3>
          <p>
            {{
              activeTab === 'cart'
                ? '从研究结果中选择商品，再加入本地清单。'
                : '从研究结果中选择商品后，点心形图标即可收藏。'
            }}
          </p>
          <NButton type="primary" secondary size="small" @click="routerPushByKey('chat')">去研究商品</NButton>
        </section>

        <footer class="cart-panel-footer">
          <div class="cart-total-row">
            <button class="select-all" type="button" :disabled="!activeItems.length" @click="toggleSelectAll">
              <span :class="{ checked: allItemsSelected }">
                <icon-material-symbols:check-rounded v-if="allItemsSelected" />
              </span>
              全选
            </button>
            <div>
              <span>服务能力</span>
              <strong>未提供价格、库存或下单</strong>
            </div>
          </div>
          <NButton
            v-if="activeTab === 'favorites'"
            block
            type="primary"
            :disabled="!selectedCount"
            @click="addFavoritesToCart"
          >
            加入本地清单 ({{ selectedCount }})
          </NButton>
          <NButton v-else block disabled>当前商品检索接口不支持结算</NButton>
        </footer>
      </aside>
    </main>
  </div>
</template>

<style scoped>
.cart-route-shell {
  display: flex;
  height: 100%;
  min-width: 0;
  min-height: 0;
  flex: 1;
  overflow: hidden;
  padding: 20px 28px 28px !important;
  background: #f5f8fc;
}

.cart-stage {
  display: flex;
  width: 100%;
  height: 100%;
  min-height: 100%;
  justify-content: center;
  color: #292d39;
}

.cart-panel {
  display: flex;
  width: min(900px, 100%);
  min-width: 0;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid #e4e8ef;
  border-radius: 20px;
  background: #fff;
  box-shadow: 0 14px 34px rgb(76 88 118 / 0.08);
}

.cart-panel-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 25px 24px 16px;
}

.cart-panel-header h2 {
  margin: 0;
  color: #2d3140;
  font-size: 18px;
  line-height: 1.3;
}

.cart-panel-header p {
  margin: 5px 0 0;
  color: #8b91a0;
  font-size: 12px;
}

.cart-tabs {
  display: flex;
  gap: 24px;
  margin: 0 24px;
  border-bottom: 1px solid #e8ebf1;
}

.cart-tabs button {
  position: relative;
  padding: 0 0 11px;
  border: 0;
  background: transparent;
  color: #8a91a0;
  cursor: pointer;
  font: inherit;
  font-size: 13px;
  font-weight: 650;
}

.cart-tabs button::after {
  position: absolute;
  right: 0;
  bottom: -1px;
  left: 0;
  height: 2px;
  background: transparent;
  content: '';
}

.cart-tabs button.active {
  color: #343847;
}

.cart-tabs button.active::after {
  background: rgb(var(--primary-color));
}

.cart-tabs span {
  margin-left: 3px;
  color: #a0a6b4;
  font-size: 12px;
}

.cart-tabs button.active span {
  color: rgb(var(--primary-color));
}

.cart-item-list {
  min-height: 0;
  flex: 1;
  overflow-y: auto;
  padding: 16px 16px 18px;
}

.cart-item {
  display: grid;
  grid-template-columns: 18px 60px minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  min-height: 80px;
  padding: 9px 10px;
  border: 1px solid #edf0f4;
  border-radius: 14px;
  background: #fff;
  transition:
    border-color 160ms ease,
    background-color 160ms ease,
    box-shadow 160ms ease;
}

.cart-item + .cart-item {
  margin-top: 9px;
}

.cart-item.is-selected {
  border-color: rgb(var(--primary-color) / 0.32);
  background: #fcfcff;
  box-shadow: 0 8px 18px rgb(80 88 157 / 0.06);
}

.cart-checkbox,
.select-all > span {
  display: inline-flex;
  width: 17px;
  height: 17px;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  border: 1.5px solid #cbd1dd;
  border-radius: 50%;
  background: #fff;
  color: #fff;
}

.cart-checkbox {
  padding: 0;
  cursor: pointer;
}

.cart-checkbox:hover,
.select-all:not(:disabled):hover > span {
  border-color: rgb(var(--primary-color) / 0.7);
}

.cart-checkbox svg,
.select-all > span svg {
  font-size: 13px;
}

.cart-item.is-selected .cart-checkbox,
.select-all > span.checked {
  border-color: rgb(var(--primary-color));
  background: rgb(var(--primary-color));
}

.cart-item-image {
  display: grid;
  width: 60px;
  height: 60px;
  place-items: center;
  overflow: hidden;
  border-radius: 10px;
  background-color: #edf0ff;
  color: #6870a7;
  font-size: 25px;
}

.cart-item-image.is-generic {
  background-color: #edf0ff;
}

.cart-item-copy {
  min-width: 0;
}

.cart-item-copy span {
  display: block;
  overflow: hidden;
  color: rgb(var(--primary-color));
  font-size: 11px;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cart-item-copy h3 {
  display: -webkit-box;
  margin: 3px 0 4px;
  overflow: hidden;
  color: #363a47;
  font-size: 13px;
  font-weight: 650;
  letter-spacing: -0.01em;
  line-height: 1.35;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.cart-item-copy strong {
  color: #2f3340;
  font-size: 13px;
  font-weight: 700;
}

.cart-remove {
  align-self: center;
  padding: 4px 1px;
  border: 0;
  background: transparent;
  color: #9ca2ae;
  cursor: pointer;
  font: inherit;
  font-size: 11px;
}

.cart-remove:hover {
  color: #767e8f;
}

.cart-empty {
  display: flex;
  min-height: 0;
  flex: 1;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 44px 28px;
  text-align: center;
}

.cart-empty-icon {
  display: grid;
  width: 48px;
  height: 48px;
  place-items: center;
  border-radius: 15px;
  background: #f0f1ff;
  color: rgb(var(--primary-color));
  font-size: 24px;
}

.cart-empty h3 {
  margin: 15px 0 6px;
  color: #3a3e4b;
  font-size: 16px;
}

.cart-empty p {
  max-width: 250px;
  margin: 0 0 18px;
  color: #8a91a0;
  font-size: 12px;
  line-height: 1.65;
}

.cart-panel-footer {
  flex: 0 0 auto;
  padding: 14px 18px 18px;
  border-top: 1px solid #e8ebf1;
  background: #fff;
  box-shadow: 0 -12px 24px rgb(82 91 119 / 0.04);
}

.cart-total-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 27px;
  margin-bottom: 12px;
}

.select-all {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 0;
  border: 0;
  background: transparent;
  color: #747b8a;
  cursor: pointer;
  font: inherit;
  font-size: 12px;
}

.select-all:disabled {
  cursor: default;
  opacity: 0.52;
}

.cart-total-row > div {
  display: flex;
  align-items: baseline;
  gap: 6px;
}

.cart-total-row > div span {
  color: #858c9b;
  font-size: 12px;
}

.cart-total-row > div strong {
  color: #303440;
  font-size: 17px;
  letter-spacing: -0.025em;
}

@media (max-width: 720px) {
  .cart-route-shell {
    padding: 0 !important;
  }

  .cart-stage {
    display: block;
    height: auto;
    min-height: 100%;
    width: 100%;
  }

  .cart-panel {
    width: 100%;
    min-height: 100%;
    border: 0;
    border-radius: 0;
  }
}

@media (min-width: 721px) {
  .cart-item {
    grid-template-columns: 20px 70px minmax(0, 1fr) auto;
    min-height: 90px;
    gap: 13px;
    padding: 10px 14px;
  }

  .cart-item-image {
    width: 70px;
    height: 70px;
  }

  .cart-item-copy h3 {
    font-size: 14px;
  }

  .cart-item-copy strong {
    font-size: 14px;
  }
}

@media (max-width: 390px) {
  .cart-panel-header {
    padding: 21px 18px 14px;
  }

  .cart-tabs {
    margin: 0 18px;
  }

  .cart-item-list {
    padding: 14px 12px;
  }

  .cart-item {
    grid-template-columns: 18px 54px minmax(0, 1fr) auto;
    gap: 8px;
    padding: 8px;
  }

  .cart-item-image {
    width: 54px;
    height: 54px;
  }

  .cart-panel-footer {
    padding: 13px 14px 15px;
  }
}
</style>
