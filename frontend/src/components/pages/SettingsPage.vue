<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { getSystemDiagnostics } from "../../api/client";
import type { SystemDiagnostics } from "../../types/operations";
import EmptyState from "../common/EmptyState.vue";
import StatusBadge from "../common/StatusBadge.vue";

const diagnostics = ref<SystemDiagnostics | null>(null);
const loading = ref(false);
const error = ref<string | null>(null);

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

onMounted(load);
</script>

<template>
  <main class="workspace-main">
    <header class="workspace-heading">
      <div>
        <p class="eyebrow">READ-ONLY DIAGNOSTICS</p>
        <h1>设置与诊断</h1>
        <p>检查运行环境、策略版本和秘密配置状态。为避免泄露，此处只显示是否已配置。</p>
      </div>
      <button type="button" class="button button--secondary" :disabled="loading" @click="load">刷新诊断</button>
    </header>

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
          <div><dt>验证后端</dt><dd>{{ diagnostics.validation_backends.filter(item => item.available).length }} / {{ diagnostics.validation_backends.length }} 可用</dd></div>
        </dl>
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
      <EmptyState v-else icon="⚙" title="诊断信息不可用" description="请确认后端服务正在运行，然后重新读取诊断信息。" action-label="重试" @action="load" />
    </section>
    <footer class="app-footer"><span>只读诊断 · 秘密不回显 · 配置由环境管理</span><span><i /> System Health</span></footer>
  </main>
</template>
