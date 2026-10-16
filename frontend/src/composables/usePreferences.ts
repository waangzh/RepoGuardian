import { ref, watch } from "vue";
function saved(key: string): string | null {
  try { return localStorage.getItem(key); } catch { return null; }
}
const theme = ref(saved("rg-theme") === "dark" ? "dark" : "light");
const density = ref(saved("rg-density") === "compact" ? "compact" : "comfortable");
for (const [key, value] of [["theme", theme], ["density", density]] as const) {
  watch(value, next => {
    document.documentElement.dataset[key] = next;
    try { localStorage.setItem(`rg-${key}`, next); } catch { /* Retain in-memory preferences. */ }
  }, { immediate: true });
}
export function usePreferences() {
  return { theme, density, toggleTheme: () => { theme.value = theme.value === "light" ? "dark" : "light"; } };
}
