<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from "vue";
import { deleteReview, listReviews } from "../../api/client";
import type { ReviewTask } from "../../types/review";
import type { ReviewHistoryTask } from "../../types/operations";
import { historyConclusion } from "../../utils/reviewHistoryPresentation";
import AppSelect from "../common/AppSelect.vue";
import EmptyState from "../common/EmptyState.vue";
import StatusBadge from "../common/StatusBadge.vue";
import { runtimeLabel, runtimeReport, taskRuntimeSummary } from "../../utils/taskRuntimePresentation";
const props = defineProps<{ busy?: boolean; deleteTask?: typeof deleteReview }>();

const emit = defineEmits<{
  open: [taskId: string];
  create: [];
  deleted: [taskId: string];
}>();

const tasks = ref<ReviewHistoryTask[]>([]);
const total = ref(0);
const page = ref(1);
const pageSize = 12;
const status = ref("");
const loading = ref(false);
const error = ref<string | null>(null);
const search = ref("");
const visibleTasks = computed(() => tasks.value);
let controller: AbortController | null = null;
let disposed = false;
onBeforeUnmount(() => { disposed = true; controller?.abort(); });
const deleteTarget = ref<ReviewHistoryTask | null>(null);
const deleting = ref(false);
const deleteError = ref("");
const feedback = ref("");
const deleteDialog = ref<HTMLDialogElement | null>(null);
const cancelButton = ref<HTMLButtonElement | null>(null);
const refreshButton = ref<HTMLButtonElement | null>(null);
const dialogTitleId = useId();
let deleteTrigger: HTMLElement | null = null;
const canDelete = (task: ReviewTask) => ["completed", "completed_with_warnings", "failed", "cancelled"].includes(task.status);

async function requestDelete(task: ReviewHistoryTask, event: Event) {
  if (props.busy || deleting.value || !canDelete(task)) return;
  deleteTrigger = event.currentTarget as HTMLElement;
  deleteTarget.value = task;
  deleteError.value = "";
  feedback.value = "";
  await nextTick();
  if (disposed || !deleteTarget.value) return;
  deleteDialog.value?.showModal();
  cancelButton.value?.focus();
}

function finishDeleteDialog() {
  deleteDialog.value?.close();
  deleteTarget.value = null;
  (deleteTrigger?.isConnected ? deleteTrigger : refreshButton.value)?.focus();
}

function cancelDelete() {
  if (!deleting.value) finishDeleteDialog();
}

async function confirmDelete() {
  const target = deleteTarget.value;
  if (!target || deleting.value || props.busy) return;
  deleting.value = true;
  deleteError.value = "";
  const request = new AbortController();
  const timeout = window.setTimeout(() => request.abort(), 20_000);
  try {
    // 生产入口由 App 持有删除请求；页面卸载后仍能清理匹配的当前任务。
    await (props.deleteTask ?? deleteReview)(target.id, request.signal);
    if (disposed) return;
    emit("deleted", target.id);
    tasks.value = tasks.value.filter(item => item.id !== target.id);
    feedback.value = "已删除本地审查记录。";
    finishDeleteDialog();
    await load();
  } catch (err) {
    if (disposed) return;
    deleteError.value = request.signal.aborted || err instanceof TypeError
      ? "未能确认删除结果，请重试；重复请求不会重复删除。"
      : err instanceof Error ? err.message : "删除失败，请稍后重试。";
  } finally {
    window.clearTimeout(timeout);
    deleting.value = false;
    if (!disposed) {
      await nextTick();
      (deleteTarget.value ? cancelButton.value : refreshButton.value)?.focus();
    }
  }
}

const statusOptions = [
  { value: "", label: "全部状态" },
  { value: "completed", label: "已完成" },
  { value: "completed_with_warnings", label: "已结束 · 有限制" },
  { value: "failed", label: "失败" },
  { value: "pending", label: "等待中" },
  { value: "queued", label: "已入队" },
  { value: "running", label: "运行中" },
  { value: "planning", label: "规划中" },
  { value: "reviewing", label: "审查中" },
  { value: "validating", label: "验证中" },
  { value: "cancelled", label: "已取消" },
];

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)));

async function load() {
  controller?.abort();
  const pending = new AbortController();
  controller = pending;
  loading.value = true;
  error.value = null;
  try {
    const result = await listReviews({ q: search.value.trim() || undefined, status: status.value || undefined, page: page.value, pageSize, signal: pending.signal });
    if (pending.signal.aborted) return;
    tasks.value = result.items;
    total.value = result.total;
    const lastPage = Math.max(1, Math.ceil(result.total / pageSize));
    if (page.value > lastPage) { page.value = lastPage; await load(); }
  } catch (err) {
    if (pending.signal.aborted) return;
    error.value = err instanceof Error ? err.message : "读取审查历史失败";
  } finally {
    if (controller === pending) loading.value = false;
  }
}

function formatDate(value: string) {
  if (!Number.isFinite(new Date(value).getTime())) return "时间未知";
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
  if (task.engine === "task_review_v1") {
    const report = runtimeReport(task);
    if (!report) return "未知";
    const scope = taskRuntimeSummary(report).scope;
    return `${scope.examined} / ${scope.total} 目标`;
  }
  const coverage = task.coverage;
  return coverage?.eligible_files ? `${coverage.reviewed_files} / ${coverage.eligible_files}` : "—";
}

// 执行状态只描述任务；“有限制”必须给出具体类型，避免把计量异常读成代码风险。
function executionLabel(task: ReviewTask) {
  return task.status === "completed_with_warnings" ? "已结束 · 有限制" : undefined;
}
function executionLimit(task: ReviewTask) {
  if (task.status !== "completed_with_warnings") return "";
  const report = runtimeReport(task);
  if (!report) return "限制类型未知";
  const summary = taskRuntimeSummary(report);
  const parts: string[] = [];
  if (summary.scope.incomplete) parts.push(`${summary.scope.incomplete} 项检查未完成`);
  if (summary.investigations.insufficient_evidence) parts.push(`${summary.investigations.insufficient_evidence} 项调查证据不足`);
  if (summary.investigations.not_executed) parts.push(`${summary.investigations.not_executed} 项调查未执行`);
  if (summary.investigations.failed) parts.push(`${summary.investigations.failed} 项调查失败`);
  if (summary.investigations.incomplete) parts.push(`${summary.investigations.incomplete} 项调查已开始但未完成`);
  if (summary.findings.unknown) parts.push(`${summary.findings.unknown} 项发现状态未知`);
  if (summary.usage.integrity !== "complete") parts.push("模型用量记录不完整");
  if (["budget_exhausted", "deadline", "no_progress"].includes(report.termination_reason)) parts.push(runtimeLabel(report.termination_reason));
  return parts.join(" · ") || "限制类型未知";
}

function unresolvedCount(task: ReviewTask) {
  if (task.engine === "task_review_v1") {
    const report = runtimeReport(task);
    return report ? taskRuntimeSummary(report).findings.unresolved : "未知";
  }
  return task.issues.filter((issue) => ["candidate", "unresolved", "needs_human"].includes(issue.status)).length;
}

watch([status, search], () => {
  page.value = 1;
  void load();
});

onMounted(load);
</script>

<template>
  <main class="workspace-main">
    <header class="workspace-heading">
      <div>
        <h1>审查历史</h1>
        <p>回看每个 PR 的本轮结论、执行状态与验证范围。结论仅适用于已保存的审查快照，不代表最新提交或合并批准。</p>
      </div>
      <button type="button" class="button button--primary" :disabled="busy" @click="emit('create')">发起新审查</button>
    </header>

    <section class="workspace-toolbar" aria-label="审查历史筛选">
      <div class="workspace-stat"><strong>{{ total }}</strong><span>累计任务</span></div>
      <label class="rg-history-search"><span>搜索全部历史</span><input v-model="search" class="input" type="search" :disabled="deleting" placeholder="PR 链接、仓库、标题或任务 ID" /></label>
      <div class="compact-field">
        <span>任务状态</span>
        <AppSelect v-model="status" :options="statusOptions" :disabled="deleting" compact aria-label="任务状态" />
      </div>
      <button ref="refreshButton" type="button" class="button button--secondary button--compact" :disabled="loading || deleting" @click="load">刷新</button>
    </section>

    <p v-if="error" class="workspace-error" role="alert">{{ error }}</p>
    <p v-if="feedback" role="status">{{ feedback }}</p>
    <section class="history-panel panel" :aria-busy="loading">
      <div v-if="loading && tasks.length === 0" class="workspace-loading">正在读取审查记录…</div>
      <EmptyState
        v-else-if="visibleTasks.length === 0"
        icon="history"
        title="暂无匹配的审查任务"
        description="创建第一条审查任务，或调整当前状态筛选。"
        action-label="发起审查"
        @action="emit('create')"
      />
      <div v-else class="history-list">
        <article v-for="task in visibleTasks" :key="task.id" class="history-row">
          <div class="history-row__state">
            <span class="history-conclusion-label">本轮结论</span>
            <strong class="history-conclusion" :class="`history-conclusion--${historyConclusion(task).tone}`" :title="historyConclusion(task).description">{{ historyConclusion(task).title }}</strong>
            <span class="history-execution">执行：<StatusBadge :status="task.status" :label="executionLabel(task)" /></span>
            <small v-if="executionLimit(task)" class="history-limit">{{ executionLimit(task) }}</small>
          </div>
          <div class="history-row__identity">
            <span>{{ taskRepo(task) }}</span>
            <strong>{{ taskTitle(task) }}</strong>
            <code>{{ task.id }} · Head {{ task.pr?.head.sha?.slice(0, 12) || '未知' }}</code>
          </div>
          <dl class="history-row__metrics">
            <div><dt>覆盖</dt><dd>{{ taskCoverage(task) }}</dd></div>
            <div><dt>发现记录</dt><dd>{{ task.engine === 'task_review_v1' ? runtimeReport(task)?.findings.length ?? '未知' : task.issues.length }}</dd></div>
            <div><dt>未确认</dt><dd>{{ unresolvedCount(task) }}</dd></div>
          </dl>
          <div class="history-row__time"><span>{{ formatDate(task.updated_at) }}</span><small>最近更新</small></div>
          <div class="history-row__actions">
            <button type="button" class="button button--secondary button--compact" :disabled="busy || deleting" @click="emit('open', task.id)">查看详情</button>
            <button type="button" class="button button--ghost button--compact history-delete" :disabled="busy || deleting || !canDelete(task)" :aria-label="`删除审查记录 ${taskTitle(task)}`" :title="canDelete(task) ? '删除这条本地审查记录' : '请先停止审查，等待后台作业结束后再删除'" @click="requestDelete(task, $event)">删除</button>
          </div>
        </article>
      </div>
    </section>

    <nav v-if="total > pageSize" class="pagination" aria-label="审查历史分页">
      <button type="button" class="button button--secondary button--compact" :disabled="page <= 1 || loading || deleting" @click="page--; load()">上一页</button>
      <span>第 {{ page }} / {{ pageCount }} 页</span>
      <button type="button" class="button button--secondary button--compact" :disabled="page >= pageCount || loading || deleting" @click="page++; load()">下一页</button>
    </nav>
    <dialog ref="deleteDialog" class="history-delete-dialog" :aria-labelledby="dialogTitleId" :aria-busy="deleting" @cancel.prevent="cancelDelete" @click.self="cancelDelete">
      <h2 :id="dialogTitleId">删除这条审查记录？</h2>
      <template v-if="deleteTarget">
        <p class="history-delete-dialog__identity">{{ taskRepo(deleteTarget) }} · {{ taskTitle(deleteTarget) }}</p>
        <p class="history-delete-dialog__meta">审查时间：{{ formatDate(deleteTarget.created_at) }}<br />任务 ID：<code>{{ deleteTarget.id }}</code></p>
        <p>删除后将无法在 RepoGuardian 中查看本次审查报告。已发布到 GitHub 的评论不会删除。</p>
      </template>
      <p v-if="deleteError" class="workspace-error" role="alert">{{ deleteError }}</p>
      <footer>
        <button ref="cancelButton" type="button" class="button button--secondary" :disabled="deleting" @click="cancelDelete">取消</button>
        <button type="button" class="button button--danger" :disabled="deleting || busy" @click="confirmDelete">{{ deleting ? "正在删除…" : "确认删除" }}</button>
      </footer>
    </dialog>
    <footer class="app-footer"><span>本地审查记录管理 · 不修改 GitHub 评论或目标仓库</span><span><i /> RepoGuardian Audit</span></footer>
  </main>
</template>

<style scoped>
.history-row__actions { display: flex; flex-wrap: wrap; gap: 6px; justify-content: flex-end; }
.history-delete { color: var(--danger); }
.history-limit { display: block; margin-top: 2px; color: var(--warning); font-size: .72rem; overflow-wrap: anywhere; }
.history-delete-dialog { width: min(500px, calc(100vw - 32px)); box-sizing: border-box; padding: 24px; border: 1px solid var(--border); border-radius: 14px; background: var(--surface); color: var(--text); box-shadow: 0 18px 60px rgb(0 0 0 / .2); }
.history-delete-dialog::backdrop { background: rgb(0 0 0 / .4); }
.history-delete-dialog h2 { margin: 0 0 16px; font-size: 1.25rem; }
.history-delete-dialog p { line-height: 1.7; overflow-wrap: anywhere; }
.history-delete-dialog__identity { font-weight: 700; }
.history-delete-dialog__meta { color: var(--text-muted); font-size: .8125rem; }
.history-delete-dialog footer { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 10px; margin-top: 24px; }
@media (max-width: 540px) { .history-row__actions { justify-content: flex-start; } }
</style>
