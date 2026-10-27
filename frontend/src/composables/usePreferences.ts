import { ref, watch } from 'vue';

function saved(key: string) {
  try { return localStorage.getItem(key); } catch { return null; }
}
const theme = ref<'light' | 'dark'>(saved('rg-theme') === 'dark' ? 'dark' : 'light');
const density = ref<'comfortable' | 'compact'>(saved('rg-density') === 'compact' ? 'compact' : 'comfortable');
for (const [key, value] of [['theme', theme], ['density', density]] as const) {
  watch(value, next => {
    if (typeof document !== 'undefined') document.documentElement.dataset[key] = next;
    try { localStorage.setItem(`rg-${key}`, next); } catch { /* Keep session preferences. */ }
  }, { immediate: true });
}
export function usePreferences() {
  return { theme, density, toggleTheme: () => { theme.value = theme.value === 'light' ? 'dark' : 'light'; } };
}
