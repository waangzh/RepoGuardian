import { ref, watch, type InjectionKey } from "vue";
import { getPrDiscussions } from "../api/client";
import type { DiscussionItem, DiscussionStats } from "../types/prDiscussion";

export function usePrDiscussions(reviewId: () => string) {
  const items = ref<DiscussionItem[]>([]), stats = ref<DiscussionStats | null>(null);
  const loading = ref(false), error = ref(""), ready = ref(false);
  const selected = ref<DiscussionItem | null>(null), detailLoading = ref(false), detailError = ref("");
  const commentOffset = ref(0);
  const details = new Map<string, DiscussionItem>();
  let listController: AbortController | undefined, detailController: AbortController | undefined;
  let listPromise: Promise<void> | undefined;
  function clearSelection() {
    detailController?.abort(); selected.value = null; detailLoading.value = false; detailError.value = "";
  }
  function cancel() {
    listController?.abort(); detailController?.abort();
    if (detailLoading.value) selected.value = null;
    listPromise = undefined; loading.value = false; detailLoading.value = false;
  }
  function reset() {
    cancel(); items.value = []; stats.value = null; selected.value = null;
    ready.value = false; error.value = ""; detailError.value = ""; commentOffset.value = 0; details.clear();
  }
  watch(reviewId, reset, { flush: "sync" });
  async function load() {
    if (ready.value) return;
    if (listPromise) return listPromise;
    const id = reviewId(), controller = new AbortController();
    listController = controller; loading.value = true; error.value = "";
    const current = () => !controller.signal.aborted && id === reviewId();
    listPromise = (async () => {
      try {
        const first = await getPrDiscussions(id, { page: 1, page_size: 100 }, controller.signal);
        const all = [...first.items];
        for (let page = 2; all.length < first.total; page++) {
          const next = await getPrDiscussions(id, { page, page_size: 100 }, controller.signal);
          if (!next.items.length) throw new Error("Incomplete discussion snapshot");
          all.push(...next.items);
        }
        // 列表不携带标题；读取首条冻结原文与摘要，最多四个并发请求。
        let cursor = 0;
        await Promise.all(Array.from({ length: Math.min(4, all.length) }, async () => {
          while (cursor < all.length && current()) {
            const index = cursor++, item = all[index]!;
            const page = await getPrDiscussions(id, { discussion_id: item.id, comment_limit: 1 }, controller.signal);
            const detail = page.items[0];
            if (!detail || detail.id !== item.id) throw new Error("Discussion missing");
            all[index] = detail;
            if (current() && detail.comment_count <= 1) details.set(`${item.id}:0`, detail);
          }
        }));
        if (current()) { items.value = all; stats.value = first.stats; ready.value = true; }
      } catch {
        if (current()) { error.value = "讨论快照加载失败，请重试；未将失败视为无讨论。"; controller.abort(); }
      } finally {
        if (listController === controller) { loading.value = false; listPromise = undefined; }
      }
    })();
    return listPromise;
  }
  async function select(item: DiscussionItem, offset = 0) {
    detailController?.abort();
    const id = reviewId(), controller = new AbortController();
    detailController = controller; detailError.value = ""; commentOffset.value = offset;
    selected.value = item; detailLoading.value = true;
    const key = `${item.id}:${offset}`;
    try {
      let detail = details.get(key);
      if (!detail) {
        const result = await getPrDiscussions(id, { discussion_id: item.id, comment_offset: offset, comment_limit: 20 }, controller.signal);
        detail = result.items[0];
        if (!detail || detail.id !== item.id) throw new Error("Discussion missing");
      }
      if (!controller.signal.aborted && id === reviewId()) { details.set(key, detail); selected.value = detail; }
    } catch {
      if (!controller.signal.aborted && id === reviewId()) detailError.value = "讨论详情加载失败，请重试。";
    } finally { if (detailController === controller) detailLoading.value = false; }
  }
  return { items, stats, loading, error, ready, selected, detailLoading, detailError, commentOffset, load, select, clearSelection, cancel, reset };
}
export type DiscussionStore = ReturnType<typeof usePrDiscussions>;
export const discussionWorkspaceKey: InjectionKey<{ store: DiscussionStore; open: () => void }> = Symbol("pr-discussion-workspace");
