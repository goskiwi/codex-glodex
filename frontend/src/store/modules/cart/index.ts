import { computed, ref } from 'vue';
import { defineStore } from 'pinia';
import { localStg } from '@/utils/storage';
import { SetupStoreId } from '@/enum';

export interface GlodexCartItem {
  id: string;
  title: string;
  category: string;
  price?: string;
  description: string;
  matches: string[];
  visualType: 'generic';
  addedAt: number;
}

export type GlodexCartItemInput = Omit<GlodexCartItem, 'addedAt'>;

/** Locally persisted shortlist of products returned by the current item-search service. */
export const useCartStore = defineStore(SetupStoreId.Cart, () => {
  const items = ref<GlodexCartItem[]>(localStg.get('glodexCartItemsV2') || []);
  const favoriteItems = ref<GlodexCartItem[]>(localStg.get('glodexFavoriteItemsV2') || []);
  const count = computed(() => items.value.length);
  const favoriteCount = computed(() => favoriteItems.value.length);

  function persist() {
    localStg.set('glodexCartItemsV2', items.value);
  }

  function persistFavorites() {
    localStg.set('glodexFavoriteItemsV2', favoriteItems.value);
  }

  function has(itemId: string) {
    return items.value.some(item => item.id === itemId);
  }

  function add(item: GlodexCartItemInput) {
    if (has(item.id)) return;

    items.value = [{ ...item, addedAt: Date.now() }, ...items.value];
    persist();
  }

  function remove(itemId: string) {
    items.value = items.value.filter(item => item.id !== itemId);
    persist();
  }

  function toggle(item: GlodexCartItemInput) {
    if (has(item.id)) {
      remove(item.id);
      return;
    }

    add(item);
  }

  function hasFavorite(itemId: string) {
    return favoriteItems.value.some(item => item.id === itemId);
  }

  function addFavorite(item: GlodexCartItemInput) {
    if (hasFavorite(item.id)) return;

    favoriteItems.value = [{ ...item, addedAt: Date.now() }, ...favoriteItems.value];
    persistFavorites();
  }

  function removeFavorite(itemId: string) {
    favoriteItems.value = favoriteItems.value.filter(item => item.id !== itemId);
    persistFavorites();
  }

  function toggleFavorite(item: GlodexCartItemInput) {
    if (hasFavorite(item.id)) {
      removeFavorite(item.id);
      return;
    }

    addFavorite(item);
  }

  return {
    items,
    favoriteItems,
    count,
    favoriteCount,
    has,
    add,
    remove,
    toggle,
    hasFavorite,
    addFavorite,
    removeFavorite,
    toggleFavorite
  };
});
