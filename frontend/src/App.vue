<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { getReviewCapabilities, cancelReview, createReview, getAvailableModels, getReport, getReview, retryReviewUnit, subscribeToEvents } from "./api/client";
import { usePreviewJob } from './composables/usePreviewJob';
import PreviewProgress from './components/review/PreviewProgress.vue';
import type { PreviewJob } from './types/previewJob';
import AppIcon from "./components/common/AppIcon.vue";
import AppHeader from "./components/layout/AppHeader.vue";
import AppSidebar from "./components/layout/AppSidebar.vue";
import ReviewHistoryPage from "./components/pages/ReviewHistoryPage.vue";
import SettingsPage from "./components/pages/SettingsPage.vue";
import NewReviewPage from "./components/pages/NewReviewPage.vue";
import ReviewPlanPage from "./components/pages/ReviewPlanPage.vue";
import ReviewWorkspace from "./components/review/ReviewWorkspace.vue";
import { reviewSections, type ReviewSection } from "./types/workspace";
import { validPullRequestUrl } from "./utils/workspacePresentation";
import type { AppPage, ProviderModelInfo } from "./types/operations";
import type { ReviewMode, ReviewPreviewResponse, ReviewTask, ValidationBackend } from "./types/review";
import { reviewCompletionMessage } from "./utils/taskRuntimePresentation";
import { usePreferences } from "./composables/usePreferences";
const { initialReviewPlan } = usePreferences();

type NoticeTone = "info" | "success" | "warning" | "danger";
interface TaskNotice {
  tone: NoticeTone;
  title: string;
  message: string;
  actionLabel?: string;
}

const activePage = ref<AppPage>("dashboard");
const reviewSection = ref<ReviewSection>("overview");
const mobileOpen = ref(false);
const opening = ref(false);
const pageTitle = computed(() => activePage.value === 'history' ? '审查历史' : activePage.value === 'settings' ? '系统设置' : task.value ? reviewSections.find(item => item.id === reviewSection.value)?.label || '当前审查' : preview.value ? '审查方案' : '新建审查');
watch([activePage, reviewSection], () => { mobileOpen.value = false; });
function navigateReview(section: ReviewSection) { activePage.value = 'dashboard'; reviewSection.value = section; mobileOpen.value = false; }
const prUrl = ref("");
const model = ref("");
const models = ref<ProviderModelInfo[]>([]);
const defaultModel = ref("");
const modelsLoading = ref(false);
const modelsError = ref<string | null>(null);
const mode = ref<ReviewMode>("review");
const generatePatches = ref(false);
const validationBackend = ref<ValidationBackend>("none");
import type { ReviewCapabilities, ReviewPlanOptions } from './types/reviewPlan';
const capabilities = ref<ReviewCapabilities | null>(null);
const draftPlan = ref<ReviewPlanOptions | undefined>();
const planDirty = ref(false);
let restoringPreview = false;
watch(model, () => { if (!restoringPreview) { planDirty.value = true; cancelPreview(); } });
watch(prUrl, () => { if (!restoringPreview) { draftPlan.value = undefined; planDirty.value = true; } });
const preview = ref<ReviewPreviewResponse | null>(null);
function changePlan(plan: ReviewPlanOptions) { draftPlan.value = plan; planDirty.value = true; }
const previewTracker = usePreviewJob();
const { job: previewJob, tracking: previewing, connectionError: previewConnectionError } = previewTracker;
const retryingUnitId = ref<string | null>(null);
const task = ref<ReviewTask | null>(null);
const report = ref<string | null>(null);
const error = ref<string | null>(null);
const submitting = ref(false);
const cancelling = ref(false);
const notice = ref<TaskNotice | null>(null);
let pollTimer: number | undefined;
let refreshTimer: number | undefined;
let noticeTimer: number | undefined;
let eventSource: EventSource | undefined;
let refreshPromise: Promise<ReviewTask | null> | null = null;
let refreshTaskId: string | null = null;
let reportPromise: Promise<string | null> | null = null;
let reportTaskId: string | null = null;
let trackingTaskId: string | null = null;
let previewRequestId = 0;
const terminalNotifiedTaskIds = new Set<string>();

watch(mode, (next) => {
  if (restoringPreview) return;
  generatePatches.value = next !== "review";
  validationBackend.value = "none";
});

watch([prUrl, mode, generatePatches, validationBackend], () => {
  if (restoringPreview) return;
  preview.value = null;
  cancelPreview();
});

async function loadAvailableModels() {
  modelsLoading.value = true;
  modelsError.value = null;
  try {
    const catalog = await getAvailableModels();
    models.value = catalog.models;
    defaultModel.value = catalog.default_model;
  } catch {
    models.value = [];
    modelsError.value = "模型列表加载失败，将使用后端默认模型";
  } finally {
    modelsLoading.value = false;
  }
}

onMounted(() => { void restorePreview(); void loadAvailableModels(); void getReviewCapabilities().then(value => { capabilities.value = value; }).catch(() => { error.value = '能力声明加载失败，请刷新后重试。'; }); });

const statusText = computed(() => {
  if (!task.value) return "等待输入";
  const labels: Record<string, string> = {
    pending: "等待审查", queued: "等待审查", planning: "正在准备", reviewing: "正在审查",
    resolving_evidence: "解析证据", verifying_issues: "核验问题", generating_patches: "生成补丁",
    validating: "验证补丁", waiting_for_human: "历史审查未完成", completed: "审查已完成",
    completed_with_warnings: "已完成，有警告", failed: "任务失败", cancelled: "任务已取消",
  };
  return labels[task.value.status] ?? `未知状态：${task.value.status}`;
});

const taskActive = computed(() => Boolean(task.value && !isTerminalStatus(task.value.status)));
const taskTerminal = computed(() => Boolean(task.value && isTerminalStatus(task.value.status)));
const taskDuration = computed(() => {
  if (!task.value || !taskTerminal.value) return "";
  const start = new Date(task.value.created_at).getTime();
  const end = new Date(task.value.updated_at).getTime();
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return "";
  const seconds = Math.max(0, Math.round((end - start) / 1000));
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
});

async function submitReview() {
  if (submitting.value || previewing.value || taskActive.value || !preview.value || planDirty.value || !preview.value.preview_id || !validPullRequestUrl(prUrl.value)) return;
  clearAll(false);
  error.value = null;
  report.value = null;
  task.value = null;
  submitting.value = true;
  reviewSection.value = "overview";
  try {
    const created = await createReview(prUrl.value.trim(), model.value.trim(), mode.value, generatePatches.value, validationBackend.value, preview.value);
    previewTracker.detach(true);
    trackingTaskId = created.task_id;
    showNotice({
      tone: "info",
      title: "审查任务已创建",
      message: `任务 ${created.task_id.slice(0, 8)} 已进入队列，正在准备审查。`,
    });
    const currentTask = await refreshTask(created.task_id);
    if (currentTask !== null && !isTerminalStatus(currentTask.status)) subscribeOrPoll(created.task_id);
  } catch (err) {
    error.value = err instanceof Error ? err.message : "创建任务失败";
  } finally {
    submitting.value = false;
  }
}

async function cancelCurrentReview() {
  if (!task.value || !taskActive.value) return;
  cancelling.value = true;
  try {
    await cancelReview(task.value.id);
    showNotice({ tone: "warning", title: "取消请求已提交", message: "正在停止当前审查任务。" });
    await refreshTask(task.value.id);
  } catch (err) {
    error.value = err instanceof Error ? err.message : "取消任务失败";
  } finally {
    cancelling.value = false;
  }
}

async function loadPreview() {
  if (previewing.value || submitting.value || taskActive.value || !validPullRequestUrl(prUrl.value)) return;
  error.value = null;
  cancelPreview();
  const requestId = ++previewRequestId;
  try {
    const next = await previewTracker.start({ pr_url: prUrl.value.trim(), mode: mode.value,
      generate_patches: generatePatches.value, validation_backend: validationBackend.value,
      model: model.value.trim() || null, plan: draftPlan.value ?? initialReviewPlan(capabilities.value) });
    if (previewRequestId === requestId && next) applyPreviewJob(next);
  } catch (err) {
    if (previewRequestId !== requestId) return;
    error.value = err instanceof Error ? err.message : "Preview 失败";
  }
}

function applyPreviewJob(value: PreviewJob) {
  if (value.status === 'completed' && value.result) {
    preview.value = value.result;
    draftPlan.value = value.result.effective_plan || undefined;
    planDirty.value = false;
  } else if (value.status === 'consumed' && value.consumed_task_id) {
    previewTracker.detach(true);
    void openHistoricalReview(value.consumed_task_id);
  } else {
    error.value = value.error || (value.status === 'cancelled' ? '预览已取消，可重新分析。' : null);
  }
}

async function restorePreview() {
  const requestId = ++previewRequestId;
  error.value = null;
  try {
    const value = await previewTracker.resume(async current => {
      restoringPreview = true;
      try {
        prUrl.value = current.request.pr_url;
        model.value = current.request.model || '';
        mode.value = current.request.mode;
        generatePatches.value = current.request.generate_patches;
        validationBackend.value = current.request.validation_backend;
        draftPlan.value = current.request.plan;
        await nextTick();
      } finally { restoringPreview = false; }
    });
    if (previewRequestId === requestId && value) applyPreviewJob(value);
  } catch (err) {
    if (previewRequestId === requestId) error.value = err instanceof Error ? err.message : '恢复预览失败';
  }
}

async function cancelPreviewJob() {
  try { await previewTracker.cancel(); }
  catch (err) { error.value = err instanceof Error ? err.message : '取消预览失败'; }
}

async function retryUnit(unitId: string) {
  if (!task.value) return;
  retryingUnitId.value = unitId;
  error.value = null;
  try {
    await retryReviewUnit(task.value.id, unitId);
    await refreshTask(task.value.id);
  } catch (err) {
    error.value = err instanceof Error ? err.message : "Unit 重试失败";
  } finally {
    retryingUnitId.value = null;
  }
}

async function openHistoricalReview(taskId: string) {
  if (submitting.value || opening.value) return;
  opening.value = true;
  reviewSection.value = 'overview';
  clearAll(false);
  trackingTaskId = taskId;
  activePage.value = "dashboard";
  error.value = null;
  report.value = null;
  task.value = null;
  const currentTask = await refreshTask(taskId, false);
  if (currentTask !== null && !isTerminalStatus(currentTask.status)) subscribeOrPoll(taskId);
  opening.value = false;
}

function subscribeOrPoll(taskId: string) {
  trackingTaskId = taskId;
  try {
    eventSource = subscribeToEvents(taskId, {
      onStepProgress: () => scheduleRefresh(taskId),
      onPatchUpdate: () => scheduleRefresh(taskId),
      onDone: () => {
        clearScheduledRefresh();
        void refreshTask(taskId).then((current) => {
          if (current && !isTerminalStatus(current.status)) scheduleRefresh(taskId, 300);
        });
      },
      onError: () => {
        if (!task.value || !isTerminalStatus(task.value.status)) startPolling(taskId);
      },
    });
  } catch {
    startPolling(taskId);
  }
}

function scheduleRefresh(taskId: string, delay = 250) {
  if (refreshTimer !== undefined) return;
  refreshTimer = window.setTimeout(() => {
    refreshTimer = undefined;
    void refreshTask(taskId);
  }, delay);
}

function clearScheduledRefresh() {
  if (refreshTimer !== undefined) {
    window.clearTimeout(refreshTimer);
    refreshTimer = undefined;
  }
}

function startPolling(taskId: string) {
  if (pollTimer !== undefined) return;
  pollTimer = window.setInterval(() => void refreshTask(taskId), 1800);
}

function isTerminalStatus(status: ReviewTask["status"]) {
  return ["completed", "completed_with_warnings", "failed", "cancelled", "waiting_for_human"].includes(status);
}

function refreshTask(taskId: string, notifyLifecycle = true): Promise<ReviewTask | null> {
  if (refreshPromise && refreshTaskId === taskId) return refreshPromise;
  refreshTaskId = taskId;
  const pending = performRefresh(taskId, notifyLifecycle).finally(() => {
    if (refreshPromise === pending) {
      refreshPromise = null;
      refreshTaskId = null;
    }
  });
  refreshPromise = pending;
  return pending;
}

async function performRefresh(taskId: string, notifyLifecycle: boolean): Promise<ReviewTask | null> {
  try {
    if (trackingTaskId !== taskId) return null;
    const previousStatus = task.value?.id === taskId ? task.value.status : null;
    const next = await getReview(taskId);
    if (trackingTaskId !== taskId) return null;
    task.value = next;
    if (next.status === "completed" || next.status === "completed_with_warnings") {
      clearPolling();
      await loadReportOnce(next);
    }
    if (next.status === "failed") {
      clearPolling();
      error.value = next.error || "任务失败";
    }
    if (next.status === "cancelled") clearPolling();
    if (
      notifyLifecycle
      && isTerminalStatus(next.status)
      && !isTerminalStatus(previousStatus || "")
      && !terminalNotifiedTaskIds.has(next.id)
    ) {
      terminalNotifiedTaskIds.add(next.id);
      showTerminalNotice(next);
    }
    return next;
  } catch (err) {
    if (trackingTaskId !== taskId) return null;
    clearPolling();
    error.value = err instanceof Error ? err.message : "读取任务失败";
    return null;
  }
}

async function loadReportOnce(next: ReviewTask): Promise<void> {
  if (next.report_markdown) {
    report.value = next.report_markdown;
    reportTaskId = next.id;
    return;
  }
  if (reportTaskId === next.id && report.value) return;
  if (reportPromise && reportTaskId === next.id) {
    await reportPromise;
    return;
  }
  reportTaskId = next.id;
  reportPromise = getReport(next.id)
    .then((markdown) => {
      if (trackingTaskId === next.id) report.value = markdown;
      return markdown;
    })
    .catch((err) => {
      if (trackingTaskId === next.id) error.value = err instanceof Error ? `任务已完成，但报告加载失败：${err.message}` : "任务已完成，但报告加载失败";
      return null;
    })
    .finally(() => { if (reportTaskId === next.id) reportPromise = null; });
  await reportPromise;
}

function showTerminalNotice(next: ReviewTask) {
  const completed = ["completed", "completed_with_warnings"].includes(next.status);
  showNotice({
    tone: next.status === "failed" ? "danger" : next.status === "completed" ? "success" : "warning",
    title: next.status === "failed" ? "审查任务失败" : next.status === "cancelled" ? "审查任务已取消" : "审查已完成",
    message: completed
      ? reviewCompletionMessage(next)
      : (next.error || "任务已停止。"),
    actionLabel: completed ? "查看结果" : undefined,
  }, 7000);
  document.title = completed ? "审查完成 · RepoGuardian" : "任务需要关注 · RepoGuardian";
}

function showNotice(next: TaskNotice, duration = 5000) {
  if (noticeTimer !== undefined) window.clearTimeout(noticeTimer);
  notice.value = next;
  noticeTimer = window.setTimeout(() => {
    notice.value = null;
    noticeTimer = undefined;
  }, duration);
}

function dismissNotice() {
  if (noticeTimer !== undefined) window.clearTimeout(noticeTimer);
  noticeTimer = undefined;
  notice.value = null;
}

function showReviewResult() {
  activePage.value = "dashboard";
  reviewSection.value = "overview";
  window.scrollTo({ top: 0, behavior: "smooth" });
  dismissNotice();
}

function startNewReview() {
  if (taskActive.value || submitting.value || opening.value) return;
  clearAll();
  task.value = null;
  report.value = null;
  preview.value = null;
  error.value = null;
  prUrl.value = "";
  draftPlan.value = undefined;
  planDirty.value = false;
  activePage.value = "dashboard";
  reviewSection.value = "overview";
  mobileOpen.value = false;
}

function clearPolling() {
  if (pollTimer !== undefined) {
    window.clearInterval(pollTimer);
    pollTimer = undefined;
  }
  if (eventSource) {
    eventSource.close();
    eventSource = undefined;
  }
  clearScheduledRefresh();
}

function clearAll(forgetPreview = true) {
  cancelPreview(forgetPreview);
  clearPolling();
  trackingTaskId = null;
  reportTaskId = null;
  reportPromise = null;
  document.title = "RepoGuardian";
}

function cancelPreview(forget = true) {
  previewRequestId += 1;
  previewTracker.detach(forget);
}
onBeforeUnmount(() => {
  clearAll(false);
  dismissNotice();
});
</script>

<template>
  <div class="app rg-app" @keydown.esc="mobileOpen = false">
    <div class="toast-region" aria-live="polite" aria-atomic="true">
      <section v-if="notice" class="task-toast" :data-tone="notice.tone">
        <span class="task-toast__icon"><AppIcon :name="notice.tone === 'success' ? 'check-circle' : notice.tone === 'info' ? 'info' : 'alert'" :size="19" /></span>
        <div><strong>{{ notice.title }}</strong><p>{{ notice.message }}</p></div>
        <button v-if="notice.actionLabel" type="button" class="button button--ghost button--compact" @click="showReviewResult">{{ notice.actionLabel }}</button>
        <button type="button" class="icon-button task-toast__close" aria-label="关闭通知" @click="dismissNotice"><AppIcon name="x" :size="15" /></button>
      </section>
    </div>
    <button v-if="mobileOpen" type="button" class="rg-nav-backdrop" aria-label="关闭导航遮罩" @click="mobileOpen = false" />
    <AppSidebar v-model="activePage" :section="reviewSection" :has-review="Boolean(task)" :task-active="taskActive" :mobile-open="mobileOpen" :busy="submitting || opening" @section="navigateReview" @create="startNewReview" @close="mobileOpen = false" />
    <div class="main">
      <AppHeader :title="pageTitle" :status-text="statusText" :menu-open="mobileOpen" @menu="mobileOpen = !mobileOpen" />
      <main v-if="activePage === 'dashboard'" id="workspace-content" class="page">
        <p v-if="error" class="notice danger" role="alert">{{ error }}</p>
        <PreviewProgress v-if="!task && !submitting" :job="previewJob" :tracking="previewing" :connection-error="previewConnectionError" @resume="restorePreview" @cancel="cancelPreviewJob" />
        <div v-if="opening || (submitting && !task)" class="card card-pad" role="status">{{ opening ? '正在打开审查记录…' : '正在校验 PR 快照并创建审查任务…' }}</div>
        <ReviewWorkspace v-else-if="task" :key="task.id" :task="task" :report="report" :section="reviewSection" :status-text="statusText" :duration="taskDuration" :cancelling="cancelling" :retrying-unit-id="retryingUnitId" @navigate="navigateReview" @cancel="cancelCurrentReview" @create="startNewReview" @retry="retryUnit" />
        <ReviewPlanPage v-else-if="preview" v-model:model="model" :preview="preview" :capabilities="capabilities" :dirty="planDirty" :previewing="previewing" @change="changePlan" @repreview="loadPreview" :pr-url="prUrl" :models="models" :default-model="defaultModel" :models-loading="modelsLoading" :models-error="modelsError" :submitting="submitting" @submit="submitReview" @back="preview = null" />
        <NewReviewPage v-else v-model:pr-url="prUrl" :loading="previewing" :busy="submitting || opening" @preview="loadPreview" @open="openHistoricalReview" @history="activePage = 'history'" />
      </main>
      <ReviewHistoryPage v-else-if="activePage === 'history'" :busy="submitting || opening || taskActive" @open="openHistoricalReview" @create="startNewReview" />
      <SettingsPage v-else :capabilities="capabilities" />
    </div>
  </div>
</template>
