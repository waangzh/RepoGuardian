import { ref } from 'vue';
import { createPreviewJob, getPreviewJob, cancelPreviewJob } from '../api/client';
import type { PreviewJob, PreviewRequest } from '../types/previewJob';

const STORAGE_KEY = 'rg-preview-job';
const pending = (job: PreviewJob) => job.status === 'queued' || job.status === 'running';

/** 首帧据此决定是否先显示恢复态，避免刷新时闪回新建页。 */
export function hasSavedPreview(): boolean {
  try { return Boolean(localStorage.getItem(STORAGE_KEY)); } catch { return false; }
}

export function usePreviewJob(api = { create: createPreviewJob, get: getPreviewJob, cancel: cancelPreviewJob }, interval = 1000) {
  const job = ref<PreviewJob | null>(null);
  const tracking = ref(false);
  const connectionError = ref('');
  let controller: AbortController | null = null;
  let identity: string | null = null;
  let generation = 0;

  function saved() {
    try { const id = localStorage.getItem(STORAGE_KEY); return id && /^[a-f0-9]{32}$/.test(id) ? id : null; }
    catch { return null; }
  }
  function remember(id: string | null) {
    try { if (id) localStorage.setItem(STORAGE_KEY, id); else localStorage.removeItem(STORAGE_KEY); }
    catch { /* 存储不可用时仍跟踪本次会话。 */ }
  }
  function detach(forget = false) {
    generation++;
    controller?.abort();
    controller = null;
    tracking.value = false;
    if (forget) { remember(null); identity = null; job.value = null; connectionError.value = ''; }
  }
  function pause(signal: AbortSignal) {
    return new Promise<void>(resolve => {
      const finish = () => { clearTimeout(timer); signal.removeEventListener('abort', finish); resolve(); };
      const timer = setTimeout(finish, interval);
      signal.addEventListener('abort', finish, { once: true });
      if (signal.aborted) finish();
    });
  }
  async function follow(id: string, input?: PreviewRequest, restore?: (value: PreviewJob) => Promise<void>) {
    detach();
    identity = id;
    remember(id);
    const current = ++generation;
    const owner = new AbortController();
    controller = owner;
    tracking.value = true;
    connectionError.value = '';
    try {
      let value = input ? await api.create(id, input, owner.signal) : await api.get(id, owner.signal);
      if (current !== generation) return null;
      if (restore) await restore(value);
      while (current === generation) {
        job.value = value;
        if (!pending(value)) return value;
        await pause(owner.signal);
        if (current !== generation) return null;
        value = await api.get(id, owner.signal);
      }
      return null;
    } catch (error) {
      if (current !== generation || owner.signal.aborted) return null;
      connectionError.value = error instanceof Error ? error.message : '连接中断';
      throw error;
    } finally {
      if (current === generation) { tracking.value = false; controller = null; }
    }
  }
  async function resume(restore?: (value: PreviewJob) => Promise<void>) {
    const id = identity ?? saved();
    return id ? follow(id, undefined, restore) : null;
  }
  async function cancel() {
    const id = identity ?? saved();
    if (!id) return;
    const value = await api.cancel(id);
    // 用户可能在取消请求返回前启动另一份预览。
    if (identity === id) { detach(); job.value = value; connectionError.value = ''; }
  }
  return { job, tracking, connectionError, detach, resume, cancel,
    start: (input: PreviewRequest) => {
      job.value = null;
      return follow(crypto.randomUUID().replaceAll('-', ''), input);
    } };
}
