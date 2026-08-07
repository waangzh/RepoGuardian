<script setup lang="ts">
import { onMounted, ref } from "vue";
import { getValidationBackends } from "../../api/client";
import type { ValidationBackendInfo } from "../../types/operations";
import EmptyState from "../common/EmptyState.vue";
import StatusBadge from "../common/StatusBadge.vue";

const backends = ref<ValidationBackendInfo[]>([]);
const loading = ref(false);
const error = ref<string | null>(null);

function healthStatus(backend: ValidationBackendInfo) {
  return backend.health_status === "healthy"
    ? "passed"
    : backend.health_status === "degraded"
      ? "warning"
      : "failed";
}

function healthLabel(backend: ValidationBackendInfo) {
  return { healthy: "健康", degraded: "需配置", unavailable: "不可用" }[backend.health_status];
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

async function load() {
  loading.value = true;
  error.value = null;
  try {
    backends.value = await getValidationBackends();
  } catch (err) {
    error.value = err instanceof Error ? err.message : "读取验证后端失败";
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
        <p class="eyebrow">EXECUTION BOUNDARY</p>
        <h1>验证后端</h1>
        <p>查看服务端允许的验证通道、可用 Profile 与安全边界。此页面不会执行目标仓库代码。</p>
      </div>
      <button type="button" class="button button--secondary" :disabled="loading" @click="load">重新探测</button>
    </header>

    <div class="boundary-note">
      <span aria-hidden="true">⌁</span>
      <p><strong>执行边界由服务端固定策略控制</strong>前端只能选择已注册后端和 Profile，不能提交任意 Shell 命令。</p>
    </div>
    <p v-if="error" class="workspace-error" role="alert">{{ error }}</p>

    <section v-if="backends.length" class="backend-grid" :aria-busy="loading">
      <article v-for="backend in backends" :key="backend.name" class="backend-card panel" :data-health="backend.health_status">
        <header>
          <div class="backend-card__mark" aria-hidden="true">{{ backend.name === 'none' ? '○' : backend.name === 'project_ci' ? 'CI' : backend.name === 'user_runner' ? 'UR' : 'GV' }}</div>
          <div><span>{{ backend.name }}</span><h2>{{ backend.display_name }}</h2></div>
          <StatusBadge :status="healthStatus(backend)" :label="healthLabel(backend)" />
        </header>
        <p class="backend-card__boundary">{{ backend.safety_boundary }}</p>
        <dl class="backend-facts">
          <div><dt>可用状态</dt><dd>{{ backend.available ? "可被任务选择" : "当前不可选择" }}</dd></div>
          <div><dt>运行目标代码</dt><dd>{{ backend.executes_untrusted_code ? "是，受策略隔离" : "否" }}</dd></div>
          <div><dt>需要用户配置</dt><dd>{{ backend.requires_user_configuration ? "需要" : "不需要" }}</dd></div>
          <div v-if="backend.registered_runner_count != null"><dt>已注册 Runner</dt><dd>{{ backend.registered_runner_count }}</dd></div>
        </dl>
        <div class="backend-tags">
          <span v-for="profile in backend.supported_profiles" :key="profile">{{ profile }}</span>
          <span v-if="backend.supported_profiles.length === 0" class="is-muted">无可用 Profile</span>
        </div>
        <p v-if="backend.unavailable_reason" class="backend-reason">{{ backend.unavailable_reason }}</p>
        <footer><span>探测于 {{ formatDate(backend.last_health_check_at) }}</span><code>{{ backend.documentation_url }}</code></footer>
      </article>
    </section>
    <section v-else class="panel">
      <div v-if="loading" class="workspace-loading">正在探测验证能力…</div>
      <EmptyState v-else icon="server" title="未发现验证后端" description="请确认后端服务可用，并检查验证后端注册配置。" action-label="重试" @action="load" />
    </section>
    <footer class="app-footer"><span>Profile 白名单 · 租约约束 · 结果可追溯</span><span><i /> Validation Control</span></footer>
  </main>
</template>
