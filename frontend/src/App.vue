<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, toRaw, watch } from 'vue';
import { cancelReview, createReview, getAvailableModels, getReview, getReviewCapabilities, resumeReview, subscribeToReview } from './api/client';
import type { AppPage } from './types/operations';
import type { ReviewPreviewResponse, ReviewTask } from './types/review';
import type { ReviewCapabilities, ReviewPlanOptions } from './types/reviewPlan';
import { reviewPrimarySections, reviewSecondarySections, type ReviewSection } from './types/workspace';
import { hasSavedPreview, usePreviewJob } from './composables/usePreviewJob';
import { defaultPlan, phaseLabel, terminal } from './utils/nativeReview';
import AppSidebar from './components/layout/AppSidebar.vue';
import AppHeader from './components/layout/AppHeader.vue';
import NewReviewPage from './components/pages/NewReviewPage.vue';
import PreviewPage from './components/pages/PreviewPage.vue';
import ReviewPlanPage from './components/pages/ReviewPlanPage.vue';
import ReviewHistoryPage from './components/pages/ReviewHistoryPage.vue';
import SettingsPage from './components/pages/SettingsPage.vue';
import ReviewWorkspace from './components/review/ReviewWorkspace.vue';

const page = ref<AppPage>('dashboard'), section = ref<ReviewSection>('overview'), mobileOpen = ref(false);
const prUrl = ref(''), model = ref(''), models = ref<string[]>([]);
const capabilities = ref<ReviewCapabilities | null>(null);
const options = ref<ReviewPlanOptions>({ ...defaultPlan(), focus: [...defaultPlan().focus] });
const preview = ref<ReviewPreviewResponse | null>(null), previewModel = ref(''), previewUrl = ref('');
const task = ref<ReviewTask | null>(null), trackedId = ref<string | null>(null);
const error = ref(''), busy = ref(false), opening = ref(false), restoring = ref(true);
const previewStage = ref(hasSavedPreview());
const tracker = usePreviewJob();
const { job, tracking, connectionError } = tracker;
let controller: AbortController | null = null;
let timer: ReturnType<typeof setTimeout> | null = null;
let closeStream: (() => void) | null = null;
let generation = 0, catalogGeneration = 0;
let disposed = false;
const taskActive = computed(() => !!task.value && !terminal(task.value.status));
const planDirty = computed(() => !!preview.value && (previewModel.value !== model.value || previewUrl.value !== prUrl.value
  || JSON.stringify(toRaw(options.value)) !== JSON.stringify(preview.value.effective_plan)));
const pageTitle = computed(() => page.value === 'history' ? '审查历史' : page.value === 'settings' ? '系统设置'
  : task.value ? [...reviewPrimarySections,...reviewSecondarySections].find(item => item.id === section.value)?.label || '当前审查'
  : preview.value ? '确认审查方案' : previewStage.value ? '准备审查方案' : '新建审查');
const statusText = computed(() => tracking.value ? '准备固定快照中' : task.value ? phaseLabel(task.value.phase) : '只读静态审查');
function forgetTask() {
  generation++; controller?.abort(); closeStream?.(); closeStream = null;
  if (timer) clearTimeout(timer); timer = null;
  task.value = null; trackedId.value = null; opening.value = false; busy.value = false; restoring.value = false;
  try { localStorage.removeItem('rg-review-task'); } catch { /* Session-only tracking. */ }
}
function newReview() {
  forgetTask(); tracker.detach(true); preview.value = null; previewStage.value = false;
  error.value = ''; page.value = 'dashboard'; section.value = 'overview'; mobileOpen.value = false; prUrl.value = '';
}
function backToInput() {
  tracker.detach(true); preview.value = null; previewStage.value = false; error.value = '';
}
function navigate(value: ReviewSection) { page.value = 'dashboard'; section.value = value; mobileOpen.value = false; }
function rereview() {
  const url = task.value?.pr_url, plan = task.value?.configuration.options;
  newReview(); if (url) prUrl.value = url; if (plan) options.value = structuredClone(toRaw(plan));
}
async function loadModels() {
  const owner = ++catalogGeneration;
  try { const value = await getAvailableModels(); if (owner === catalogGeneration) models.value = value.models.map(item => item.id); }
  catch { /* A manually entered model remains available. */ }
}
async function follow(id: string) {
  if (disposed) return;
  tracker.detach(); preview.value = null; previewStage.value = false; page.value = 'dashboard';
  mobileOpen.value = false;
  if (trackedId.value !== id) { task.value = null; section.value = 'overview'; }
  trackedId.value = id; opening.value = true; busy.value = false;
  closeStream?.(); closeStream = null; controller?.abort(); if (timer) clearTimeout(timer);
  const owner = ++generation, abort = new AbortController(); controller = abort;
  let refreshing = false;
  try { localStorage.setItem('rg-review-task',id); } catch { /* Session-only tracking. */ }
  async function refresh() {
    if (owner !== generation || refreshing) return;
    refreshing = true; if (timer) clearTimeout(timer); timer = null;
    try {
      const result = await getReview(id,abort.signal); if (owner !== generation) return;
      if (result.id !== id) throw new Error('返回的记录不属于当前审查任务');
      task.value = result; error.value = '';
      if (!terminal(result.status)) timer = setTimeout(refresh,1200);
      else { closeStream?.(); closeStream = null; }
    } catch (cause) {
      if (owner !== generation || abort.signal.aborted) return;
      error.value = cause instanceof Error ? cause.message : '连接中断'; timer = setTimeout(refresh,3000);
    } finally { refreshing = false; if (owner === generation) opening.value = false; }
  }
  await refresh();
  if (owner === generation && task.value && !terminal(task.value.status)) closeStream = subscribeToReview(id,() => { void refresh(); });
}
async function prepare() {
  if (busy.value || tracking.value) return;
  const plan = structuredClone(toRaw(options.value));
  plan.scope.primary_files = plan.scope.mode === 'selected' ? plan.scope.primary_files.map(path => path.trim()).filter(Boolean) : [];
  if (!plan.focus.length || (plan.scope.mode === 'selected' && !plan.scope.primary_files.length)) { error.value = '请选择审查重点，并填写指定范围内的文件路径。'; return; }
  const owner = generation, selectedModel = model.value;
  preview.value = null; previewStage.value = true; page.value = 'dashboard'; error.value = ''; busy.value = true;
  try {
    const result = await tracker.start({pr_url:prUrl.value,model:selectedModel || undefined,plan});
    if (owner !== generation) return;
    if (result?.status === 'completed' && result.result) {
      preview.value = result.result; options.value = structuredClone(toRaw(result.result.effective_plan));
      previewModel.value = selectedModel; previewUrl.value = prUrl.value;
    } else if (result?.error) error.value = result.error;
  } catch (cause) { if (owner === generation) error.value = cause instanceof Error ? cause.message : '方案准备失败'; }
  finally { if (owner === generation) busy.value = false; }
}
async function restorePreview() {
  const owner = generation;
  try {
    const result = await tracker.resume(async value => {
      if (owner !== generation) return;
      prUrl.value = value.request.pr_url; model.value = value.request.model || '';
      if (value.request.plan) options.value = structuredClone(toRaw(value.request.plan));
      previewStage.value = true;
    });
    if (owner !== generation) return;
    if (result?.status === 'completed' && result.result) {
      preview.value = result.result; options.value = structuredClone(toRaw(result.result.effective_plan));
      previewModel.value = model.value; previewUrl.value = prUrl.value;
    } else if (result?.status === 'consumed' && result.consumed_task_id) await follow(result.consumed_task_id);
  } catch (cause) { if (owner === generation) error.value = cause instanceof Error ? cause.message : '恢复预览失败'; }
}
async function start() {
  if (!preview.value || planDirty.value || busy.value) return;
  const owner = generation;
  busy.value = true; error.value = '';
  try {
    const result = await createReview(prUrl.value,preview.value);
    if (owner !== generation) return;
    tracker.detach(true); preview.value = null; await follow(result.task_id);
  } catch (cause) { if (owner === generation) error.value = cause instanceof Error ? cause.message : '启动失败'; }
  finally { if (!disposed && owner === generation) busy.value = false; }
}
async function stop() {
  if (!task.value || busy.value) return;
  const owner = generation, id = task.value.id;
  busy.value = true;
  try { await cancelReview(id); if (owner === generation) await follow(id); }
  catch (cause) { if (owner === generation) error.value = cause instanceof Error ? cause.message : '停止失败'; }
  finally { if (!disposed && owner === generation) busy.value = false; }
}
async function resume() {
  if (!task.value || busy.value) return;
  const owner = generation, id = task.value.id;
  busy.value = true;
  try {
    const result = await resumeReview(id);
    if (owner !== generation) return;
    if (result.id !== id) throw new Error('返回的记录不属于当前审查任务');
    await follow(id);
  }
  catch (cause) { if (owner === generation) error.value = cause instanceof Error ? cause.message : '继续失败'; }
  finally { if (!disposed && owner === generation) busy.value = false; }
}
async function cancelPreview() {
  const owner = generation;
  try { await tracker.cancel(); }
  catch (cause) { if (owner === generation) error.value = cause instanceof Error ? cause.message : '取消预览失败'; }
}
watch(page, () => { mobileOpen.value = false; });
watch(pageTitle, title => { if (typeof document !== 'undefined') document.title = `${title} · RepoGuardian`; });
onMounted(async () => {
  const owner = generation;
  void loadModels();
  try {
    const value = await getReviewCapabilities();
    if (disposed || owner !== generation) return;
    capabilities.value = value; options.value.include_pr_discussions = value.pr_discussions_default ?? false;
  }
  catch (cause) { if (disposed || owner !== generation) return; error.value = cause instanceof Error ? cause.message : '能力读取失败'; }
  let saved: string | null = null; try { saved = localStorage.getItem('rg-review-task'); } catch { /* Session-only tracking. */ }
  try { if (saved && /^[a-f0-9]{32}$/.test(saved)) await follow(saved); else await restorePreview(); }
  finally { if (!disposed) restoring.value = false; }
});
onBeforeUnmount(() => { disposed = true; generation++; catalogGeneration++; controller?.abort(); closeStream?.(); if (timer) clearTimeout(timer); tracker.detach(); });
</script>
<template>
  <div class="app rg-app" @keydown.esc="mobileOpen = false">
    <button v-if="mobileOpen" class="rg-nav-backdrop" aria-label="关闭导航遮罩" @click="mobileOpen = false" />
    <AppSidebar v-model="page" :section="section" :has-review="!!task" :has-preview="previewStage || !!preview || restoring || !!trackedId" :task-active="taskActive" :mobile-open="mobileOpen" :busy="busy || opening || tracking" @section="navigate" @create="newReview" @close="mobileOpen = false" />
    <div class="main"><AppHeader :title="pageTitle" :status-text="statusText" :menu-open="mobileOpen" @menu="mobileOpen = !mobileOpen" />
      <main id="workspace-content" class="page">
        <p v-if="error" class="notice warn space-bottom" role="alert">{{ error }}</p>
        <ReviewHistoryPage v-if="page === 'history'" :busy="busy || opening || tracking" @open="follow" @create="newReview" @deleted="task?.id === $event && forgetTask()" />
        <SettingsPage v-else-if="page === 'settings'" @saved="loadModels" />
        <template v-else>
          <ReviewWorkspace v-if="task" :key="task.id" :task="task" :section="section" :busy="busy || opening" @navigate="navigate" @cancel="stop" @resume="resume" @rereview="rereview" />
          <ReviewPlanPage v-else-if="preview" v-model:options="options" v-model:model="model" :preview="preview" :pr-url="prUrl" :capabilities="capabilities" :busy="busy" :dirty="planDirty" @submit="start" @update="prepare" @back="backToInput" />
          <section v-else-if="restoring || opening || trackedId" class="card card-pad workspace-loading" role="status"><h1>正在打开审查记录…</h1><p class="muted small">恢复已保存的任务与执行状态。</p><button v-if="trackedId && !opening" class="btn secondary space-top" @click="follow(trackedId)">重试打开任务</button></section>
          <PreviewPage v-else-if="previewStage" :job="job" :tracking="tracking" :connection-error="connectionError" :pr-url="prUrl" @resume="restorePreview" @cancel="cancelPreview" @retry="prepare" @back="backToInput" />
          <NewReviewPage v-else v-model:pr-url="prUrl" v-model:model="model" v-model:options="options" :models="models" :capabilities="capabilities" :busy="busy || tracking" @preview="prepare" @open="follow" @history="page = 'history'" />
        </template>
      </main>
    </div>
  </div>
</template>
