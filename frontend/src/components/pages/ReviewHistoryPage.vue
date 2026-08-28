<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { listReviews } from "../../api/client";
import type { ReviewTask } from "../../types/review";
import AppSelect from "../common/AppSelect.vue";
import EmptyState from "../common/EmptyState.vue";
import StatusBadge from "../common/StatusBadge.vue";

const emit = defineEmits<{
  open: [taskId: string];
  create: [];
}>();

const tasks = ref<ReviewTask[]>([]);
const total = ref(0);
const page = ref(1);
const pageSize = 12;
const status = ref("");
const loading = ref(false);
const error = ref<string | null>(null);

const statusOptions = [
  { value: "", label: "全部状态" },
  { value: "completed", label: "已完成" },
  { value: "completed_with_warnings", label: "存在警告" },
  { value: "failed", label: "失败" },
  { value: "pending", label: "等待中" },
  { value: "reviewing", label: "审查中" },
  { value: "validating", label: "验证中" },
  { value: "waiting_for_human", label: "等待人工" },
  { value: "cancelled", label: "已取消" },
];

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)));

async function load() {
  loading.value = true;
  error.value = null;
  try {
    const result = await listReviews({ status: status.value || undefined, page: page.value, pageSize });
    tasks.value = result.items;
    total.value = result.total;
  } catch (err) {
    error.value = err instanceof Error ? err.message : "读取审查历史失败";
  } finally {
    loading.value = false;
  }
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function taskTitle(task: ReviewTask) {
  return task.pr ? `#${task.pr.number} ${task.pr.title}` : task.pr_url;
}

function taskRepo(task: ReviewTask) {
  return task.pr ? `${task.pr.owner}/${task.pr.repo}` : "等待获取仓库信息";
}

function taskCoverage(task: ReviewTask) {
  const coverage = task.coverage;
  return coverage?.eligible_files ? `${coverage.reviewed_files} / ${coverage.eligible_files}` : "—";
}

function humanCount(task: ReviewTask) {
  return task.issues.filter((issue) => issue.status === "needs_human" || issue.requires_human_confirmation).length;
}

function validationStatus(task: ReviewTask) {
  const latest = task.validation.at(-1);
  if (latest) return latest.status;
  return task.validation_backend === "project_ci" && !["completed", "completed_with_warnings", "failed", "cancelled"].includes(task.status)
    ? "running"
    : "—";
}

watch(status, () => {
  page.value = 1;
  void load();
});

onMounted(load);
</script>

<template>
  <main class="workspace-main">
    <header class="workspace-heading">
      <div>
        <p class="eyebrow">AUDIT TRAIL</p>
        <h1>审查历史</h1>
        <p>按任务回看审查状态、问题与验证结果。所有记录均来自后端持久化存储。</p>
      </div>
      <button type="button" class="button button--primary" @click="emit('create')">发起新审查</button>
    </header>

    <section class="workspace-toolbar" aria-label="审查历史筛选">
      <div class="workspace-stat"><strong>{{ total }}</strong><span>累计任务</span></div>
      <div class="compact-field">
        <span>任务状态</span>
        <AppSelect v-model="status" :options="statusOptions" compact aria-label="任务状态" />
      </div>
      <button type="button" class="button button--secondary button--compact" :disabled="loading" @click="load">刷新</button>
    </section>

    <p v-if="error" class="workspace-error" role="alert">{{ error }}</p>
    <section class="history-panel panel" :aria-busy="loading">
      <div v-if="loading && tasks.length === 0" class="workspace-loading">正在读取审查记录…</div>
      <EmptyState
        v-else-if="tasks.length === 0"
        icon="history"
        title="暂无匹配的审查任务"
        description="创建第一条审查任务，或调整当前状态筛选。"
        action-label="发起审查"
        @action="emit('create')"
      />
      <div v-else class="history-list">
        <article v-for="task in tasks" :key="task.id" class="history-row">
          <div class="history-row__state"><StatusBadge :status="task.status" /></div>
          <div class="history-row__identity">
            <span>{{ taskRepo(task) }}</span>
            <strong>{{ taskTitle(task) }}</strong>
            <code>{{ task.id }}</code>
          </div>
          <dl class="history-row__metrics">
            <div><dt>覆盖</dt><dd>{{ taskCoverage(task) }}</dd></div>
            <div><dt>发现</dt><dd>{{ task.issues.length }}</dd></div>
            <div><dt>人工</dt><dd>{{ humanCount(task) }}</dd></div>
            <div><dt>验证</dt><dd class="history-validation">{{ validationStatus(task) }}</dd></div>
          </dl>
          <div class="history-row__time"><span>{{ formatDate(task.updated_at) }}</span><small>最近更新</small></div>
          <button type="button" class="button button--secondary button--compact" @click="emit('open', task.id)">查看详情</button>
        </article>
      </div>
    </section>

    <nav v-if="total > pageSize" class="pagination" aria-label="审查历史分页">
      <button type="button" class="button button--secondary button--compact" :disabled="page <= 1 || loading" @click="page--; load()">上一页</button>
      <span>第 {{ page }} / {{ pageCount }} 页</span>
      <button type="button" class="button button--secondary button--compact" :disabled="page >= pageCount || loading" @click="page++; load()">下一页</button>
    </nav>
    <footer class="app-footer"><span>审查记录只读展示 · 不触发仓库写操作</span><span><i /> RepoGuardian Audit</span></footer>
  </main>
</template>
