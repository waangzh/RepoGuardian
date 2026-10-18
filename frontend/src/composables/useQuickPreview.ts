import { ref } from 'vue';
import { getQuickPreview } from '../api/client';
import type { QuickPreview } from '../types/quickPreview';

export function useQuickPreview(fetchOverview = getQuickPreview) {
  const overview = ref<QuickPreview | null>(null);
  const loading = ref(false);
  const error = ref('');
  let controller: AbortController | null = null;
  let generation = 0;

  function reset() {
    generation++;
    controller?.abort();
    controller = null;
    loading.value = false;
    overview.value = null;
    error.value = '';
  }
  async function load(prUrl: string) {
    reset();
    const current = generation;
    const owner = new AbortController();
    controller = owner;
    loading.value = true;
    try {
      const result = await fetchOverview(prUrl, owner.signal);
      if (current === generation) overview.value = result;
    } catch (cause) {
      if (current === generation && !owner.signal.aborted) {
        error.value = cause instanceof Error ? cause.message : '获取 PR 概览失败';
      }
    } finally {
      if (current === generation) { loading.value = false; controller = null; }
    }
  }
  return { overview, loading, error, load, reset };
}
