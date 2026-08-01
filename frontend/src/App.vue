<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { createReview, getReport, getReview, previewReview, retryReviewUnit, subscribeToEvents } from "./api/client";
import ChangedFiles from "./components/ChangedFiles.vue";
import ContextPanel from "./components/ContextPanel.vue";
import IssueList from "./components/IssueList.vue";
import PatchPanel from "./components/PatchPanel.vue";
import ReportPanel from "./components/ReportPanel.vue";
import ValidationPanel from "./components/ValidationPanel.vue";
import AppHeader from "./components/layout/AppHeader.vue";
import AppSidebar from "./components/layout/AppSidebar.vue";
import ExecutionTimeline from "./components/review/ExecutionTimeline.vue";
import ReviewLauncher from "./components/review/ReviewLauncher.vue";
import ReviewMetrics from "./components/review/ReviewMetrics.vue";
import ReviewUnitsPanel from "./components/review/ReviewUnitsPanel.vue";
import type { ReviewMode, ReviewPreviewResponse, ReviewTask, ValidationBackend } from "./types/review";

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
let pollTimer: number | undefined;
let eventSource: EventSource | undefined;

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

async function submitReview() {
  clearAll();
  error.value = null;
  report.value = null;
  task.value = null;
  submitting.value = true;
  try {
    const created = await createReview(prUrl.value.trim(), model.value.trim(), mode.value, generatePatches.value, validationBackend.value);
    const currentTask = await refreshTask(created.task_id);
    if (currentTask !== null && !isTerminalStatus(currentTask.status)) subscribeOrPoll(created.task_id);
  } catch (err) {
    error.value = err instanceof Error ? err.message : "创建任务失败";
  } finally {
    submitting.value = false;
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

function subscribeOrPoll(taskId: string) {
  try {
    eventSource = subscribeToEvents(taskId, {
      onStepProgress: () => void refreshTask(taskId),
      onPatchUpdate: () => void refreshTask(taskId),
      onDone: () => window.setTimeout(() => void refreshTask(taskId), 500),
      onError: () => startPolling(taskId),
    });
  } catch {
    startPolling(taskId);
  }
}

function startPolling(taskId: string) {
  if (pollTimer !== undefined) return;
  pollTimer = window.setInterval(() => void refreshTask(taskId), 1800);
}

function isTerminalStatus(status: ReviewTask["status"]) {
  return ["completed", "completed_with_warnings", "failed", "cancelled"].includes(status);
}

async function refreshTask(taskId: string): Promise<ReviewTask | null> {
  try {
    const next = await getReview(taskId);
    task.value = next;
    if (next.status === "completed" || next.status === "completed_with_warnings") {
      clearPolling();
      report.value = await getReport(taskId);
    }
    if (next.status === "failed") {
      clearPolling();
      error.value = next.error || "任务失败";
    }
    return next;
  } catch (err) {
    clearPolling();
    error.value = err instanceof Error ? err.message : "读取任务失败";
    return null;
  }
}

function clearPolling() {
  if (pollTimer) {
    window.clearInterval(pollTimer);
    pollTimer = undefined;
  }
  if (eventSource) {
    eventSource.close();
    eventSource = undefined;
  }
}

function clearAll() { clearPolling(); }
onBeforeUnmount(clearPolling);
</script>

<template>
  <div class="app-shell">
    <AppHeader :status-text="statusText" :status-english="statusEnglish" :status="task?.status || 'idle'" />
    <div class="app-body">
      <AppSidebar />
      <ReviewLauncher
        v-model:pr-url="prUrl"
        v-model:model="model"
        v-model:mode="mode"
        v-model:generate-patches="generatePatches"
        v-model:validation-backend="validationBackend"
        :previewing="previewing"
        :submitting="submitting"
        :error="error"
        :preview="preview"
        @preview="loadPreview"
        @submit="submitReview"
      />

      <main class="dashboard-main">
        <header class="dashboard-heading">
          <div><p class="eyebrow">AI REVIEW WORKSPACE</p><h1>PR Review Control Desk</h1><p>AI 驱动的 Pull Request 审查控制台，支持审查规划、证据解析、问题验证与候选修复。</p></div>
        </header>

        <section v-if="task?.pr" class="pr-summary">
          <div class="pr-summary__identity"><span>{{ task.pr.owner }}/{{ task.pr.repo }}</span><strong>#{{ task.pr.number }} {{ task.pr.title }}</strong></div>
          <div class="pr-summary__meta"><code>{{ task.pr.base.ref }} → {{ task.pr.head.ref }}</code><span>{{ modeText }}</span><span>{{ task.model || "后端默认模型" }}</span><a :href="task.pr.html_url" target="_blank" rel="noreferrer">在 GitHub 查看 ↗</a></div>
        </section>

        <ReviewMetrics :task="task" />

        <section class="dashboard-grid">
          <ChangedFiles :files="task?.changed_files || []" />
          <ReviewUnitsPanel :units="task?.review_units || []" :results="task?.review_unit_results || []" :task-status="task?.status" :retrying-unit-id="retryingUnitId" @retry="retryUnit" />
          <ExecutionTimeline :steps="task?.steps || []" :task-status="task?.status" :mode="task?.mode || mode" :events="task?.agent_events || []" :static-results="task?.static_results || []" :test-results="task?.test_results || []" />
          <IssueList :issues="task?.issues || []" />
          <PatchPanel :patches="task?.patches || []" :validations="task?.validation || []" />
          <ValidationPanel :profile="task?.project_profile" :snapshots="task?.validation_snapshots || []" :deltas="task?.validation_deltas || []" :results="task?.validation || []" />
          <ContextPanel :snippets="task?.context_snippets || []" />
          <ReportPanel :markdown="report || task?.report_markdown" />
        </section>
        <footer class="app-footer"><span>隐私优先 · 数据不出域 · 全流程可追溯</span><span><i /> RepoGuardian v0.1.0</span></footer>
      </main>
    </div>
  </div>
</template>
