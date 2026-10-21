<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import {
  cleanupExpiredWorkspaces,
  getSystemDiagnostics,
  previewWorkspaceCleanup,
} from "../../api/client";
import type {
  SystemDiagnostics,
  WorkspaceCleanupPreview,
} from "../../types/operations";
import EmptyState from "../common/EmptyState.vue";
import StatusBadge from "../common/StatusBadge.vue";
import { usePreferences } from "../../composables/usePreferences";
import ReviewDepthSelector from "../review/ReviewDepthSelector.vue";
import type { ReviewCapabilities } from "../../types/reviewPlan";
const props = defineProps<{ capabilities: ReviewCapabilities | null }>();
const { theme, density, defaultReviewDepth, defaultPrDiscussions } = usePreferences();
const currentDepthLabel = computed(() => ({ quick: "快速初筛", standard: "标准审查", deep: "深度核验" })[defaultReviewDepth.value]);
const discussionsEnabled = computed(() => defaultPrDiscussions.value === "enabled"
  && props.capabilities?.pr_discussions_available === true);

const diagnostics = ref<SystemDiagnostics | null>(null);
const loading = ref(false);
const error = ref<string | null>(null);
const workspacePreview = ref<WorkspaceCleanupPreview | null>(null);
const workspaceScanning = ref(false);
const workspaceCleaning = ref(false);
const workspaceMessage = ref<string | null>(null);

const secretLabels: Record<string, string> = {
  github: "GitHub Token",
  llm_provider: "LLM Provider Key",
  langsmith: "LangSmith Key",
  runner_admin: "Runner Registration Token",
  github_webhook: "GitHub Webhook Secret",
};

const versionLabels: Record<string, string> = {
  config: "配置协议",
  prompt: "Prompt",
  rule: "规则集",
  tool_schema: "工具 Schema",
  review_policy: "审查策略",
  patch_policy: "补丁策略",
};

const healthyChecks = computed(() => {
  if (!diagnostics.value) return 0;
  return [
    diagnostics.value.database_schema_current,
    diagnostics.value.artifact_directory_writable,
    diagnostics.value.security_mode === "restricted",
  ].filter(Boolean).length;
});

async function load() {
  loading.value = true;
  error.value = null;
  try {
    diagnostics.value = await getSystemDiagnostics();
  } catch (err) {
    error.value = err instanceof Error ? err.message : "读取系统诊断失败";
  } finally {
    loading.value = false;
  }
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = units[0];
  for (let index = 1; index < units.length && value >= 1024; index += 1) {
    value /= 1024;
    unit = units[index];
  }
  return `${value.toFixed(value >= 10 ? 1 : 2)} ${unit}`;
}

async function scanWorkspaces() {
  workspaceScanning.value = true;
  workspaceMessage.value = null;
  error.value = null;
  try {
    workspacePreview.value = await previewWorkspaceCleanup();
  } catch (err) {
    error.value = err instanceof Error ? err.message : "扫描临时工作区失败";
  } finally {
    workspaceScanning.value = false;
  }
}

async function cleanWorkspaces() {
  const preview = workspacePreview.value;
  if (!preview || preview.eligible === 0) return;
  const confirmed = window.confirm(
    `确认删除 ${preview.eligible} 个过期临时工作区并释放约 ${formatBytes(preview.eligible_bytes)} 空间？此操作不可撤销。`,
  );
  if (!confirmed) return;

  workspaceCleaning.value = true;
  workspaceMessage.value = null;
  error.value = null;
  try {
    const result = await cleanupExpiredWorkspaces();
    workspaceMessage.value = result.removed > 0
      ? `已删除 ${result.removed} 个工作区，释放 ${formatBytes(result.reclaimed_bytes)}。`
      : "没有符合安全保留期限的工作区需要清理。";
    workspacePreview.value = await previewWorkspaceCleanup();
  } catch (err) {
    error.value = err instanceof Error ? err.message : "清理临时工作区失败";
  } finally {
    workspaceCleaning.value = false;
  }
}

onMounted(load);
</script>

<template>
  <main class="workspace-main">
    <header class="workspace-heading">
      <div>
        <p class="eyebrow">SYSTEM OPERATIONS</p>
        <h1>系统设置</h1>
        <p>检查运行环境、策略版本与配置状态，并维护本机产生的临时审查工作区。</p>
      </div>
      <button type="button" class="button button--secondary" :disabled="loading" @click="load">刷新诊断</button>
    </header>

    <section class="card card-pad rg-preferences">
      <div class="card-header"><div><h2>界面偏好</h2><p>仅保存在此浏览器中，不更改服务端审查策略</p></div></div>
      <div class="rg-preference-row"><div><strong>外观主题</strong><p>按阅读习惯切换浅色或深色</p></div><div class="segmented"><button v-for="item in ([['light', '浅色'], ['dark', '深色']] as const)" :key="item[0]" type="button" :class="{ active: theme === item[0] }" :aria-pressed="theme === item[0]" @click="theme = item[0]">{{ item[1] }}</button></div></div>
      <div class="rg-preference-row"><div><strong>信息密度</strong><p>紧凑模式减少表格与列表的间距</p></div><div class="segmented"><button v-for="item in ([['comfortable', '舒适'], ['compact', '紧凑']] as const)" :key="item[0]" type="button" :class="{ active: density === item[0] }" :aria-pressed="density === item[0]" @click="density = item[0]">{{ item[1] }}</button></div></div>
      <details class="rg-preference-row rg-depth-preference">
        <summary :aria-label="`默认审查深度：${currentDepthLabel}`">
          <div><strong>默认审查深度</strong><p>自动保存于此浏览器，仅用于新建方案；可在单次审查中调整，不影响已有方案或任务，不跨设备同步。</p></div>
          <span class="rg-depth-current">{{ currentDepthLabel }}<span class="rg-depth-chevron" aria-hidden="true">⌄</span></span>
        </summary>
        <ReviewDepthSelector v-if="capabilities" v-model="defaultReviewDepth" class="space-top" :presets="capabilities.depth_presets" label="默认审查深度" />
        <p v-else>深度能力尚未加载，请刷新页面重试。</p>
      </details>
      <div class="rg-preference-row rg-discussion-preference">
        <div><strong>审查时参考 PR 历史讨论</strong><p>仅用于新建方案，可在单次审查中覆盖；已有任务继续使用保存的选择。</p><p>开启后采集、参考并复核历史讨论，可能增加耗时与模型用量；关闭后不执行这些步骤。</p><p v-if="!capabilities">正在读取历史讨论审查能力…</p><p v-else-if="!capabilities.pr_discussions_available">服务端未开放历史讨论审查，当前为关闭。</p></div>
        <div class="segmented" role="group" aria-label="历史讨论审查默认选择">
          <button type="button" :class="{ active: discussionsEnabled }" :aria-pressed="discussionsEnabled" :disabled="!capabilities?.pr_discussions_available" @click="defaultPrDiscussions = 'enabled'">开启</button>
          <button type="button" :class="{ active: !discussionsEnabled }" :aria-pressed="!discussionsEnabled" :disabled="!capabilities" @click="defaultPrDiscussions = 'disabled'">关闭</button>
        </div>
      </div>
    </section>
    <p v-if="error" class="workspace-error" role="alert">{{ error }}</p>
    <section v-if="diagnostics" class="diagnostics-layout" :aria-busy="loading">
      <article class="diagnostic-hero panel">
        <div class="diagnostic-score"><strong>{{ healthyChecks }}/3</strong><span>核心检查通过</span></div>
        <div>
          <span>RepoGuardian</span>
          <h2>v{{ diagnostics.version }}</h2>
          <p>{{ diagnostics.provider }} / {{ diagnostics.default_model }}</p>
        </div>
        <StatusBadge :status="diagnostics.security_mode === 'restricted' ? 'passed' : 'warning'" :label="diagnostics.security_mode === 'restricted' ? '受限模式' : '本地非安全模式'" />
      </article>

      <article class="diagnostic-panel panel">
        <header><span aria-hidden="true">◎</span><div><h2>运行状态</h2><p>服务、数据库与产物目录</p></div></header>
        <dl class="diagnostic-list">
          <div><dt>后台 Worker</dt><dd><StatusBadge :status="diagnostics.worker_status === 'running' ? 'running' : 'pending'" :label="diagnostics.worker_status === 'running' ? '运行中' : '空闲'" /></dd></div>
          <div><dt>数据库 Schema</dt><dd><StatusBadge :status="diagnostics.database_schema_current ? 'passed' : 'failed'" :label="diagnostics.database_schema_current ? '最新' : '需要迁移'" /></dd></div>
          <div><dt>产物目录</dt><dd><StatusBadge :status="diagnostics.artifact_directory_writable ? 'passed' : 'failed'" :label="diagnostics.artifact_directory_writable ? '可写' : '不可写'" /></dd></div>
          <div><dt>LangSmith</dt><dd>{{ diagnostics.langsmith_enabled ? "已启用" : "未启用" }}</dd></div>
        </dl>
      </article>

      <article class="diagnostic-panel panel">
        <header><span aria-hidden="true">◆</span><div><h2>安全策略</h2><p>候选补丁与记录保留边界</p></div></header>
        <dl class="diagnostic-list">
          <div><dt>单补丁文件上限</dt><dd>{{ diagnostics.patch_max_files }} 个</dd></div>
          <div><dt>单补丁变更行上限</dt><dd>{{ diagnostics.patch_max_changed_lines }} 行</dd></div>
          <div><dt>记录保留</dt><dd>{{ diagnostics.retention_days }} 天</dd></div>
        </dl>
      </article>

      <article class="diagnostic-panel diagnostic-panel--wide workspace-maintenance panel">
        <header><span aria-hidden="true">↻</span><div><h2>临时工作区</h2><p>仅扫描并清理超过安全保留期限的非活动 clone</p></div></header>
        <div class="workspace-maintenance__body">
          <div class="workspace-maintenance__copy">
            <strong>{{ workspacePreview ? `${workspacePreview.eligible} 个可清理` : "等待安全扫描" }}</strong>
            <p v-if="workspacePreview">
              已扫描 {{ workspacePreview.scanned }} 个目录，可释放约 {{ formatBytes(workspacePreview.eligible_bytes) }}；
              活动目录 {{ workspacePreview.skipped_active }} 个，保留期内 {{ workspacePreview.skipped_recent }} 个。
            </p>
            <p v-else>扫描不会修改文件。只有达到安全 TTL 且不在活动任务中的目录才会进入清理候选。</p>
            <small v-if="workspacePreview">当前安全保留期：{{ Math.ceil(workspacePreview.ttl_seconds / 86400) }} 天</small>
            <span v-if="workspaceMessage" class="workspace-maintenance__message" role="status">{{ workspaceMessage }}</span>
          </div>
          <div class="workspace-maintenance__actions">
            <button type="button" class="button button--secondary" :disabled="workspaceScanning || workspaceCleaning" @click="scanWorkspaces">
              {{ workspaceScanning ? "扫描中…" : workspacePreview ? "重新扫描" : "扫描可清理项" }}
            </button>
            <button
              v-if="workspacePreview"
              type="button"
              class="button button--danger"
              :disabled="workspacePreview.eligible === 0 || workspaceScanning || workspaceCleaning"
              @click="cleanWorkspaces"
            >
              {{ workspaceCleaning ? "清理中…" : `确认清理 ${workspacePreview.eligible} 个目录` }}
            </button>
          </div>
        </div>
      </article>

      <article class="diagnostic-panel panel diagnostic-panel--wide">
        <header><span aria-hidden="true">⌁</span><div><h2>秘密配置</h2><p>仅显示配置状态，后端不会返回秘密值</p></div></header>
        <div class="secret-grid">
          <div v-for="(configured, name) in diagnostics.configured_secrets" :key="name">
            <span>{{ secretLabels[name] || name }}</span>
            <StatusBadge :status="configured ? 'passed' : 'pending'" :label="configured ? '已配置' : '未配置'" />
          </div>
        </div>
      </article>

      <article class="diagnostic-panel panel diagnostic-panel--wide">
        <header><span aria-hidden="true">#</span><div><h2>策略版本</h2><p>用于审查结果复现与审计</p></div></header>
        <div class="version-grid">
          <div v-for="(value, name) in diagnostics.versions" :key="name"><span>{{ versionLabels[name] || name }}</span><code>{{ value }}</code></div>
        </div>
      </article>
    </section>
    <section v-else class="panel">
      <div v-if="loading" class="workspace-loading">正在读取系统诊断…</div>
      <EmptyState v-else icon="settings" title="诊断信息不可用" description="请确认后端服务正在运行，然后重新读取诊断信息。" action-label="重试" @action="load" />
    </section>
    <footer class="app-footer"><span>秘密不回显 · 工作区安全回收 · 配置由环境管理</span><span><i /> System Health</span></footer>
  </main>
</template>

<style scoped>
.rg-depth-preference { display: block; }
.rg-depth-preference > summary { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; list-style: none; cursor: pointer; }
.rg-depth-preference > summary::-webkit-details-marker { display: none; }
.rg-depth-preference > summary:focus-visible { outline: 2px solid var(--primary); outline-offset: 4px; border-radius: 4px; }
.rg-depth-current { display: inline-flex; align-items: center; gap: 10px; color: var(--muted); font-size: .875rem; }
.rg-depth-chevron { display: inline-block; }
.rg-depth-preference[open] .rg-depth-chevron { transform: rotate(180deg); }
</style>
