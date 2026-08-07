<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { cancelReview, createReview, getReport, getReview, previewReview, retryReviewUnit, subscribeToEvents } from "./api/client";
import ChangedFiles from "./components/ChangedFiles.vue";
import ContextPanel from "./components/ContextPanel.vue";
import IssueList from "./components/IssueList.vue";
import PatchPanel from "./components/PatchPanel.vue";
import ReportPanel from "./components/ReportPanel.vue";
import ValidationPanel from "./components/ValidationPanel.vue";
import AppIcon from "./components/common/AppIcon.vue";
import StatusBadge from "./components/common/StatusBadge.vue";
import AppHeader from "./components/layout/AppHeader.vue";
import AppSidebar from "./components/layout/AppSidebar.vue";
import ReviewHistoryPage from "./components/pages/ReviewHistoryPage.vue";
import SettingsPage from "./components/pages/SettingsPage.vue";
import ValidationBackendsPage from "./components/pages/ValidationBackendsPage.vue";
import ExecutionTimeline from "./components/review/ExecutionTimeline.vue";
import ReviewLauncher from "./components/review/ReviewLauncher.vue";
import ReviewMetrics from "./components/review/ReviewMetrics.vue";
import ReviewUnitsPanel from "./components/review/ReviewUnitsPanel.vue";
import type { AppPage } from "./types/operations";
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
const terminalNotifiedTaskIds = new Set<string>();

watch(mode, (next) => {
  generatePatches.value = next !== "review";
  validationBackend.value = "none";
});

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

const modeText = computed(() => ({
  review: "只读审查",
  review_and_suggest: "审查 + 候选补丁",
  review_suggest_and_validate: "审查 + 补丁 + 验证",
}[task.value?.mode || mode.value]));

const taskActive = computed(() => Boolean(task.value && !isTerminalStatus(task.value.status)));
const taskTerminal = computed(() => Boolean(task.value && isTerminalStatus(task.value.status)));
const lifecycleTone = computed<NoticeTone>(() => {
  if (!task.value) return "info";
  if (task.value.status === "failed") return "danger";
  if (["cancelled", "completed_with_warnings", "waiting_for_human"].includes(task.value.status)) return "warning";
  if (task.value.status === "completed") return "success";
  return "info";
});
const lifecycleIcon = computed(() => {
  if (lifecycleTone.value === "success") return "check-circle";
  if (lifecycleTone.value === "danger" || lifecycleTone.value === "warning") return "alert";
  return taskActive.value ? "settings" : "info";
});
const lifecycleDescription = computed(() => {
  if (!task.value) return "";
  if (task.value.status === "completed") {
    return task.value.issues.length
      ? `审查完成，共发现 ${task.value.issues.length} 个需关注的问题。`
      : "审查完成，未发现达到报告门槛的问题。";
  }
  if (task.value.status === "completed_with_warnings") return `审查已完成，并产生 ${task.value.warnings.length} 条警告。`;
  if (task.value.status === "failed") return task.value.error || "审查未能完成，请检查错误信息后重试。";
  if (task.value.status === "cancelled") return "任务已取消，已停止继续处理。";
  if (task.value.status === "waiting_for_human") return "自动流程已暂停，正在等待人工补充信息。";
  const currentStep = [...task.value.steps].reverse().find((step) => step.status === "running");
  return currentStep?.message || "任务已创建，后台审查流程正在运行。";
});
const taskDuration = computed(() => {
  if (!task.value || !taskTerminal.value) return "";
  const start = new Date(task.value.created_at).getTime();
  const end = new Date(task.value.updated_at).getTime();
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return "";
  const seconds = Math.max(0, Math.round((end - start) / 1000));
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
});

async function submitReview() {
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
  error.value = null;
  previewing.value = true;
  try {
    preview.value = await previewReview(prUrl.value.trim(), mode.value, generatePatches.value, validationBackend.value);
  } catch (err) {
    error.value = err instanceof Error ? err.message : "Preview 失败";
  } finally {
    previewing.value = false;
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
    actionLabel: completed ? "查看报告" : undefined,
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

function scrollToReport() {
  document.getElementById("review-report")?.scrollIntoView({ behavior: "smooth", block: "start" });
  dismissNotice();
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
  clearPolling();
  trackingTaskId = null;
  reportTaskId = null;
  reportPromise = null;
  document.title = "RepoGuardian";
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
        <button v-if="notice.actionLabel" type="button" class="button button--ghost button--compact" @click="scrollToReport">{{ notice.actionLabel }}</button>
        <button type="button" class="icon-button task-toast__close" aria-label="关闭通知" @click="dismissNotice"><AppIcon name="x" :size="15" /></button>
      </section>
    </div>
    <AppHeader :status-text="statusText" :status-english="statusEnglish" :status="task?.status || 'idle'" />
    <div class="app-body" :class="{ 'app-body--wide': activePage !== 'dashboard' }">
      <AppSidebar v-model="activePage" />
      <ReviewLauncher
        v-if="activePage === 'dashboard'"
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
        :preview="preview"
        @preview="loadPreview"
        @submit="submitReview"
        @cancel="cancelCurrentReview"
      />

      <main v-if="activePage === 'dashboard'" class="dashboard-main">
        <header class="dashboard-heading">
          <div><p class="eyebrow">AI REVIEW WORKSPACE</p><h1>PR Review Control Desk</h1><p>AI 驱动的 Pull Request 审查控制台，支持审查规划、证据解析、问题验证与候选修复。</p></div>
        </header>

        <section v-if="task?.pr" class="pr-summary">
          <div class="pr-summary__identity"><span>{{ task.pr.owner }}/{{ task.pr.repo }}</span><strong>#{{ task.pr.number }} {{ task.pr.title }}</strong></div>
          <div class="pr-summary__meta"><code>{{ task.pr.base.ref }} → {{ task.pr.head.ref }}</code><span>{{ modeText }}</span><span>{{ task.model || "后端默认模型" }}</span><a :href="task.pr.html_url" target="_blank" rel="noreferrer">在 GitHub 查看 ↗</a></div>
        </section>

        <section v-if="task" class="lifecycle-bar" :data-tone="lifecycleTone" role="status" aria-live="polite">
          <span class="lifecycle-bar__icon"><AppIcon :name="lifecycleIcon" :size="21" /></span>
          <div class="lifecycle-bar__content">
            <span>任务 {{ task.id.slice(0, 8) }}</span>
            <strong>{{ statusText }}</strong>
            <p>{{ lifecycleDescription }}</p>
          </div>
          <div class="lifecycle-bar__meta">
            <StatusBadge :status="task.status" />
            <span v-if="taskDuration">总耗时 {{ taskDuration }}</span>
          </div>
          <button v-if="taskActive" type="button" class="button button--secondary button--compact" :disabled="cancelling" @click="cancelCurrentReview">{{ cancelling ? "取消中…" : "取消审查" }}</button>
          <button v-else-if="report || task.report_markdown" type="button" class="button button--primary button--compact" @click="scrollToReport">查看报告</button>
        </section>

        <section v-if="!task" class="workspace-intro-state">
          <span><AppIcon name="shield" :size="28" /></span>
          <div><h2>从一次可追溯的 PR 审查开始</h2><p>在左侧输入 GitHub PR URL。你可以先运行 Preview 确认审查范围，再启动完整审查。</p></div>
        </section>

        <template v-else>
          <ReviewMetrics :task="task" />

          <section v-if="taskActive" class="review-workbench">
            <ExecutionTimeline :steps="task.steps || []" :task-status="task.status" :mode="task.mode" :events="task.agent_events || []" :static-results="task.static_results || []" :test-results="task.test_results || []" />
            <div class="review-workbench__support">
              <ChangedFiles :files="task.changed_files || []" />
              <ReviewUnitsPanel :units="task.review_units || []" :results="task.review_unit_results || []" :task-status="task.status" :retrying-unit-id="retryingUnitId" @retry="retryUnit" />
            </div>
          </section>

          <template v-else>
            <section class="review-result-layout">
              <div class="review-result-layout__main">
                <IssueList :issues="task.issues || []" :task-status="task.status" />
                <ReportPanel v-if="report || task.report_markdown" :markdown="report || task.report_markdown" />
              </div>
              <ExecutionTimeline :steps="task.steps || []" :task-status="task.status" :mode="task.mode" :events="task.agent_events || []" :static-results="task.static_results || []" :test-results="task.test_results || []" />
            </section>

            <section class="review-support-grid">
              <ChangedFiles :files="task.changed_files || []" />
              <ReviewUnitsPanel :units="task.review_units || []" :results="task.review_unit_results || []" :task-status="task.status" :retrying-unit-id="retryingUnitId" @retry="retryUnit" />
            </section>
          </template>

          <section v-if="task.mode !== 'review'" class="review-optional-grid">
            <PatchPanel :patches="task.patches || []" :validations="task.validation || []" />
            <ValidationPanel
              v-if="task.mode === 'review_suggest_and_validate'"
              :profile="task.project_profile"
              :snapshots="task.validation_snapshots || []"
              :deltas="task.validation_deltas || []"
              :results="task.validation || []"
            />
          </section>

          <details v-if="task.context_snippets?.length" class="review-details">
            <summary><span><AppIcon name="context" :size="17" />更多审查上下文</span><small>{{ task.context_snippets.length }} 条引用片段</small></summary>
            <ContextPanel :snippets="task.context_snippets" />
          </details>
        </template>
        <footer class="app-footer"><span>隐私优先 · 数据不出域 · 全流程可追溯</span><span><i /> RepoGuardian v0.1.0</span></footer>
      </main>
      <ReviewHistoryPage v-else-if="activePage === 'history'" @open="openHistoricalReview" @create="activePage = 'dashboard'" />
      <ValidationBackendsPage v-else-if="activePage === 'validation'" />
      <SettingsPage v-else />
    </div>
  </div>
</template>
