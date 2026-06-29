<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from "vue";
import { createReview, getReview, getReport, subscribeToEvents } from "./api/client";
import ChangedFiles from "./components/ChangedFiles.vue";
import ContextPanel from "./components/ContextPanel.vue";
import IssueList from "./components/IssueList.vue";
import ReportPanel from "./components/ReportPanel.vue";
import TaskTimeline from "./components/TaskTimeline.vue";
import type { ReviewTask } from "./types/review";

const prUrl = ref("");
const model = ref("");
const task = ref<ReviewTask | null>(null);
const report = ref<string | null>(null);
const error = ref<string | null>(null);
const submitting = ref(false);
let pollTimer: number | undefined;
let eventSource: EventSource | undefined;

const statusText = computed(() => {
  if (!task.value) return "等待输入";
  return task.value.status;
});

async function submitReview() {
  clearAll();
  error.value = null;
  report.value = null;
  task.value = null;
  submitting.value = true;
  try {
    const created = await createReview(prUrl.value.trim(), model.value.trim());
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

function subscribeOrPoll(taskId: string) {
  try {
    eventSource = subscribeToEvents(taskId, {
      onStepProgress: () => {
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
  return status === "completed" || status === "failed";
}

async function refreshTask(taskId: string): Promise<ReviewTask | null> {
  try {
    const next = await getReview(taskId);
    task.value = next;
    if (next.status === "completed") {
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
          <button :disabled="submitting" type="submit">
            {{ submitting ? "提交中" : "开始审查" }}
          </button>
          <p v-if="error" class="error">{{ error }}</p>
        </form>

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
        <ContextPanel :snippets="task?.context_snippets || []" />
        <IssueList :issues="task?.issues || []" />
        <ReportPanel :markdown="report || task?.report_markdown" />
      </section>
    </section>
  </main>
</template>

