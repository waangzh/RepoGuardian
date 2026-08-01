<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import {
  createReview,
  getReview,
  getReport,
  previewReview,
  retryReviewUnit,
  subscribeToEvents,
} from "./api/client";
import AgentPanel from "./components/AgentPanel.vue";
import ChangedFiles from "./components/ChangedFiles.vue";
import ContextPanel from "./components/ContextPanel.vue";
import IssueList from "./components/IssueList.vue";
import ReportPanel from "./components/ReportPanel.vue";
import TaskTimeline from "./components/TaskTimeline.vue";
import ValidationPanel from "./components/ValidationPanel.vue";
import type {
  ReviewMode,
  ReviewPreviewResponse,
  ReviewTask,
  ValidationBackend,
} from "./types/review";

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
  validationBackend.value = next === "review_suggest_and_validate" ? "local" : "none";
});

const statusText = computed(() => {
  if (!task.value) return "等待输入";
  const labels: Record<string, string> = {
    queued: "等待审查",
    planning: "正在准备审查",
    reviewing: "正在只读审查",
    resolving_evidence: "正在补充证据",
    verifying_issues: "正在核验问题",
    generating_patches: "正在生成候选修复",
    validating: "正在验证候选修复",
    waiting_for_human: "等待人工确认",
    completed: "审查已完成",
    completed_with_warnings: "审查已完成（存在警告）",
    failed: "任务失败",
    cancelled: "任务已取消",
  };
  return labels[task.value.status] ?? `未知状态：${task.value.status}`;
});

async function submitReview() {
  clearAll();
  error.value = null;
  report.value = null;
  task.value = null;
  submitting.value = true;
  try {
    const created = await createReview(
      prUrl.value.trim(),
      model.value.trim(),
      mode.value,
      generatePatches.value,
      validationBackend.value,
    );
    const currentTask = await refreshTask(created.task_id);
    if (currentTask !== null && !isTerminalStatus(currentTask.status)) {
      subscribeOrPoll(created.task_id);
    }
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
    preview.value = await previewReview(
      prUrl.value.trim(),
      mode.value,
      generatePatches.value,
      validationBackend.value,
    );
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
      onStepProgress: () => {
        void refreshTask(taskId);
      },
      onPatchUpdate: () => {
        void refreshTask(taskId);
      },
      onDone: () => {
        window.setTimeout(() => void refreshTask(taskId), 500);
      },
      onError: () => {
        startPolling(taskId);
      },
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
  return status === "completed" || status === "completed_with_warnings" || status === "failed" || status === "cancelled";
}

function unitResult(unitId: string) {
  return task.value?.review_unit_results.find((result) => result.review_unit_id === unitId);
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

function clearAll() {
  clearPolling();
}

onBeforeUnmount(clearPolling);
</script>

<template>
  <main class="shell">
    <section class="hero">
      <div>
        <p class="eyebrow">RepoGuardian</p>
        <h1>PR Review Control Desk</h1>
      </div>
      <div class="status-chip" :data-status="task?.status || 'idle'">{{ statusText }}</div>
    </section>

    <section class="workspace">
      <aside class="left-rail">
        <form class="panel intake" @submit.prevent="submitReview">
          <h2>启动审查</h2>
          <label>
            GitHub PR URL
            <input
              v-model="prUrl"
              type="url"
              placeholder="https://github.com/owner/repo/pull/123"
              required
            />
          </label>
          <label>
            Model
            <input v-model="model" type="text" placeholder="使用后端默认模型" />
          </label>
          <label>
            审查模式
            <select v-model="mode">
              <option value="review">只读审查</option>
              <option value="review_and_suggest">审查 + 候选补丁</option>
              <option value="review_suggest_and_validate">审查 + 补丁 + 验证</option>
            </select>
          </label>
          <label v-if="mode === 'review_suggest_and_validate'">
            验证后端
            <select v-model="validationBackend">
              <option value="local">本地受控后端</option>
              <option value="gvisor">gVisor</option>
            </select>
          </label>
          <label v-if="mode !== 'review'" class="checkbox-row">
            <input v-model="generatePatches" type="checkbox" />
            生成候选补丁
          </label>
          <div class="form-actions">
            <button :disabled="previewing || !prUrl" type="button" class="secondary" @click="loadPreview">
              {{ previewing ? "分析中" : "Preview" }}
            </button>
            <button :disabled="submitting" type="submit">
              {{ submitting ? "提交中" : "开始审查" }}
            </button>
          </div>
          <p class="hint">Preview 只做确定性分析，不调用模型，也不执行目标仓库代码。</p>
          <p v-if="error" class="error">{{ error }}</p>
        </form>

        <section v-if="preview" class="panel preview-panel">
          <div class="panel-head">
            <h2>审查 Preview</h2>
            <span>{{ preview.review_units.length }} Units</span>
          </div>
          <div class="preview-metrics">
            <strong>{{ preview.included_file_count }}/{{ preview.changed_file_count }}</strong>
            <span>纳入审查文件</span>
            <strong>{{ preview.estimated_model_calls }}</strong>
            <span>预计模型调用</span>
            <strong>{{ preview.estimated_tokens.toLocaleString() }}</strong>
            <span>预计 Token</span>
          </div>
          <p>模式：{{ preview.mode }} · 候选补丁：{{ preview.patch_generation_enabled ? "开启" : "关闭" }}</p>
          <p>
            验证：{{ preview.validation_backend.name }} ·
            {{ preview.validation_backend.available ? "可用" : "不可用" }}
          </p>
          <p v-if="preview.validation_backend.unavailable_reason" class="hint">
            {{ preview.validation_backend.unavailable_reason }}
          </p>
          <div class="tag-list">
            <span v-for="tag in preview.risk_tags" :key="tag">{{ tag }}</span>
          </div>
          <details v-if="preview.review_units.length">
            <summary>查看 Unit 拆分</summary>
            <p v-for="unit in preview.review_units" :key="unit.id">
              {{ unit.primary_files.join("、") }} — {{ unit.grouping_reason }} / {{ unit.complexity }}
            </p>
          </details>
          <details v-if="preview.excluded_files.length">
            <summary>排除 {{ preview.excluded_files.length }} 个文件</summary>
            <p v-for="file in preview.excluded_files" :key="file.file_path">
              {{ file.file_path }} — {{ file.reason }}
            </p>
          </details>
        </section>

        <section class="panel" v-if="task">
          <div class="panel-head">
            <h2>任务流程</h2>
            <span>{{ task.id.slice(0, 8) }}</span>
          </div>
          <TaskTimeline :steps="task.steps" />
        </section>
      </aside>

      <section class="main-grid">
        <section v-if="task?.pr" class="panel pr-summary">
          <div>
            <span>PR #{{ task.pr.number }}</span>
            <h2>{{ task.pr.title }}</h2>
            <p>{{ task.pr.owner }}/{{ task.pr.repo }}</p>
          </div>
          <a :href="task.pr.html_url" target="_blank" rel="noreferrer">GitHub</a>
        </section>

        <ChangedFiles :files="task?.changed_files || []" />
        <section v-if="task?.review_units.length" class="panel unit-panel">
          <div class="panel-head">
            <h2>Review Units</h2>
            <span>{{ task.review_units.length }}</span>
          </div>
          <article v-for="unit in task.review_units" :key="unit.id" class="unit-row">
            <div>
              <strong>{{ unit.primary_files.join("、") }}</strong>
              <p>{{ unit.grouping_reason }} · {{ unit.complexity }}</p>
            </div>
            <span v-if="unitResult(unit.id)" :data-status="unitResult(unit.id)?.status">
              {{ unitResult(unit.id)?.status }}
            </span>
            <button
              v-if="unitResult(unit.id) && isTerminalStatus(task.status)"
              type="button"
              class="secondary compact"
              :disabled="retryingUnitId === unit.id"
              @click="retryUnit(unit.id)"
            >
              {{ retryingUnitId === unit.id ? "重试中" : "重试 Unit" }}
            </button>
          </article>
        </section>
        <AgentPanel
          :events="task?.agent_events || []"
          :static-results="task?.static_results || []"
          :patches="task?.patches || []"
          :test-results="task?.test_results || []"
        />
        <ValidationPanel
          :profile="task?.project_profile"
          :snapshots="task?.validation_snapshots || []"
          :deltas="task?.validation_deltas || []"
          :results="task?.validation || []"
        />
        <ContextPanel :snippets="task?.context_snippets || []" />
        <IssueList :issues="task?.issues || []" />
        <ReportPanel :markdown="report || task?.report_markdown" />
      </section>
    </section>
  </main>
</template>

