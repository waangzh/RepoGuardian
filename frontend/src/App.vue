<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { cancelReview, createReview, getAvailableModels, getReport, getReview, previewReview, retryReviewUnit, subscribeToEvents } from "./api/client";
import AppIcon from "./components/common/AppIcon.vue";
import AppHeader from "./components/layout/AppHeader.vue";
import AppSidebar from "./components/layout/AppSidebar.vue";
import ReviewHistoryPage from "./components/pages/ReviewHistoryPage.vue";
import SettingsPage from "./components/pages/SettingsPage.vue";
import ValidationBackendsPage from "./components/pages/ValidationBackendsPage.vue";
import ReviewLauncher from "./components/review/ReviewLauncher.vue";
import ReviewPreviewPanel from "./components/review/ReviewPreviewPanel.vue";
import ReviewViewer from "./components/review/ReviewViewer.vue";
import type { AppPage, ProviderModelInfo } from "./types/operations";
import type { ReviewMode, ReviewPreviewResponse, ReviewTask, ValidationBackend } from "./types/review";

type NoticeTone = "info" | "success" | "warning" | "danger";
interface TaskNotice {
  tone: NoticeTone;
  title: string;
  message: string;
  actionLabel?: string;
}

const activePage = ref<AppPage>("dashboard");
const prUrl = ref("");
const model = ref("");
const models = ref<ProviderModelInfo[]>([]);
const defaultModel = ref("");
const modelsLoading = ref(false);
const modelsError = ref<string | null>(null);
const mode = ref<ReviewMode>("review");
const generatePatches = ref(false);
const validationBackend = ref<ValidationBackend>("none");
const preview = ref<ReviewPreviewResponse | null>(null);
const previewing = ref(false);
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
let previewController: AbortController | null = null;
let previewRequestId = 0;
const terminalNotifiedTaskIds = new Set<string>();

watch(mode, (next) => {
  generatePatches.value = next !== "review";
  validationBackend.value = "none";
});

watch([prUrl, mode, generatePatches, validationBackend], () => {
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

onMounted(() => void loadAvailableModels());

const statusText = computed(() => {
  if (!task.value) return "等待输入";
  const labels: Record<string, string> = {
    pending: "等待审查", queued: "等待审查", planning: "正在准备", reviewing: "正在审查",
    resolving_evidence: "解析证据", verifying_issues: "核验问题", generating_patches: "生成补丁",
    validating: "验证补丁", waiting_for_human: "等待人工", completed: "审查已完成",
    completed_with_warnings: "已完成，有警告", failed: "任务失败", cancelled: "任务已取消",
  };
  return labels[task.value.status] ?? `未知状态：${task.value.status}`;
});

const statusEnglish = computed(() => {
  if (!task.value) return "Ready";
  if (["completed", "completed_with_warnings"].includes(task.value.status)) return "Completed";
  if (["failed", "cancelled"].includes(task.value.status)) return "Attention";
  if (task.value.status === "waiting_for_human") return "Human review";
  return "Running";
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
  if (submitting.value || previewing.value || taskActive.value || !prUrl.value.trim()) return;
  clearAll();
  error.value = null;
  report.value = null;
  task.value = null;
  submitting.value = true;
  try {
    const created = await createReview(prUrl.value.trim(), model.value.trim(), mode.value, generatePatches.value, validationBackend.value);
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
  if (previewing.value || submitting.value || taskActive.value || !prUrl.value.trim()) return;
  error.value = null;
  cancelPreview();
  const requestId = ++previewRequestId;
  const controller = new AbortController();
  previewController = controller;
  previewing.value = true;
  try {
    const next = await previewReview(
      prUrl.value.trim(),
      mode.value,
      generatePatches.value,
      validationBackend.value,
      controller.signal,
    );
    if (previewRequestId === requestId) preview.value = next;
  } catch (err) {
    if (controller.signal.aborted) return;
    error.value = err instanceof Error ? err.message : "Preview 失败";
  } finally {
    if (previewController === controller) {
      previewController = null;
      previewing.value = false;
    }
  }
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
  clearAll();
  trackingTaskId = taskId;
  activePage.value = "dashboard";
  error.value = null;
  report.value = null;
  task.value = null;
  const currentTask = await refreshTask(taskId, false);
  if (currentTask !== null && !isTerminalStatus(currentTask.status)) subscribeOrPoll(taskId);
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
  return ["completed", "completed_with_warnings", "failed", "cancelled"].includes(status);
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
      error.value = err instanceof Error ? `任务已完成，但报告加载失败：${err.message}` : "任务已完成，但报告加载失败";
      return null;
    })
    .finally(() => { reportPromise = null; });
  await reportPromise;
}

function showTerminalNotice(next: ReviewTask) {
  const completed = ["completed", "completed_with_warnings"].includes(next.status);
  showNotice({
    tone: next.status === "failed" ? "danger" : next.status === "completed" ? "success" : "warning",
    title: next.status === "failed" ? "审查任务失败" : next.status === "cancelled" ? "审查任务已取消" : "审查已完成",
    message: completed
      ? (next.issues.length ? `发现 ${next.issues.length} 个需关注的问题，报告已生成。` : "未发现达到报告门槛的问题，报告已生成。")
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
  window.scrollTo({ top: 0, behavior: "smooth" });
  dismissNotice();
}

function startNewReview() {
  clearAll();
  task.value = null;
  report.value = null;
  preview.value = null;
  error.value = null;
  prUrl.value = "";
  activePage.value = "dashboard";
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

function clearAll() {
  cancelPreview();
  clearPolling();
  trackingTaskId = null;
  reportTaskId = null;
  reportPromise = null;
  document.title = "RepoGuardian";
}

function cancelPreview() {
  previewRequestId += 1;
  previewController?.abort();
  previewController = null;
  previewing.value = false;
}
onBeforeUnmount(() => {
  clearAll();
  dismissNotice();
});
</script>

<template>
  <div class="app-shell">
    <div class="toast-region" aria-live="polite" aria-atomic="true">
      <section v-if="notice" class="task-toast" :data-tone="notice.tone">
        <span class="task-toast__icon"><AppIcon :name="notice.tone === 'success' ? 'check-circle' : notice.tone === 'info' ? 'info' : 'alert'" :size="19" /></span>
        <div><strong>{{ notice.title }}</strong><p>{{ notice.message }}</p></div>
        <button v-if="notice.actionLabel" type="button" class="button button--ghost button--compact" @click="showReviewResult">{{ notice.actionLabel }}</button>
        <button type="button" class="icon-button task-toast__close" aria-label="关闭通知" @click="dismissNotice"><AppIcon name="x" :size="15" /></button>
      </section>
    </div>
    <AppHeader :status-text="statusText" :status-english="statusEnglish" :status="task?.status || 'idle'" />
    <div class="app-body" :class="{ 'app-body--wide': activePage !== 'dashboard' || task }">
      <AppSidebar v-model="activePage" :has-review="Boolean(task)" />
      <ReviewLauncher
        v-if="activePage === 'dashboard' && !task"
        v-model:pr-url="prUrl"
        v-model:model="model"
        v-model:mode="mode"
        v-model:generate-patches="generatePatches"
        v-model:validation-backend="validationBackend"
        :previewing="previewing"
        :submitting="submitting"
        :active="taskActive"
        :cancelling="cancelling"
        :error="error"
        :models="models"
        :default-model="defaultModel"
        :models-loading="modelsLoading"
        :models-error="modelsError"
        @preview="loadPreview"
        @submit="submitReview"
        @cancel="cancelCurrentReview"
      />

      <main v-if="activePage === 'dashboard'" class="dashboard-main" :class="{ 'dashboard-main--viewer': task }">
        <header v-if="!task" class="dashboard-heading">
          <div><p class="eyebrow">DETERMINISTIC REVIEW PLANNING</p><h1>启动新审查</h1><p>先预览确定性变更范围，再启动一次严格只读、证据可追溯的代码审查。</p></div>
        </header>

        <ReviewPreviewPanel v-if="!task" :preview="preview" :previewing="previewing" />
        <ReviewViewer v-else :task="task" :report="report" :status-text="statusText" :task-duration="taskDuration" :cancelling="cancelling" :retrying-unit-id="retryingUnitId" @cancel="cancelCurrentReview" @new-review="startNewReview" @retry="retryUnit" />
        <footer v-if="!task" class="app-footer"><span>Preview 零模型调用 · 审查严格只读 · 全流程可追溯</span><span><i /> RepoGuardian v0.1.0</span></footer>
      </main>
      <ReviewHistoryPage v-else-if="activePage === 'history'" @open="openHistoricalReview" @create="activePage = 'dashboard'" />
      <ValidationBackendsPage v-else-if="activePage === 'validation'" />
      <SettingsPage v-else />
    </div>
  </div>
</template>
