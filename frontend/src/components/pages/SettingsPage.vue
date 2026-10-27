<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue';
import { getDiagnostics, updateSystemSettings } from '../../api/client';
import type { SystemDiagnostics } from '../../types/operations';
import { usePreferences } from '../../composables/usePreferences';
import AppIcon from '../common/AppIcon.vue';
const emit = defineEmits<{ saved: [] }>();
const { theme, density } = usePreferences();
const state = ref<SystemDiagnostics | null>(null), error = ref(''), feedback = ref(''), busy = ref(false);
const endpoint = ref(''), model = ref(''), retention = ref(30);
let disposed = false;
async function load() {
  busy.value = true; error.value = '';
  try {
    const value = await getDiagnostics(); if (disposed) return;
    state.value = value; endpoint.value = value.model_base_url; model.value = value.default_model; retention.value = value.retention_days;
  } catch (cause) { if (!disposed) error.value = cause instanceof Error ? cause.message : '读取设置失败'; }
  finally { if (!disposed) busy.value = false; }
}
async function save() {
  if (busy.value) return;
  busy.value = true; error.value = ''; feedback.value = '';
  try {
    const value = await updateSystemSettings({model_base_url:endpoint.value,model_name:model.value,retention_days:retention.value});
    if (!disposed) { state.value = value; feedback.value = '设置已保存，新的审查将使用更新后的配置。'; emit('saved'); }
  } catch (cause) { if (!disposed) error.value = cause instanceof Error ? cause.message : '保存设置失败'; }
  finally { if (!disposed) busy.value = false; }
}
onMounted(load);
onBeforeUnmount(() => { disposed = true; });
</script>
<template>
  <div class="page-head"><div><h1>系统设置</h1><p class="title-sub">管理模型连接、记录保留和本机显示偏好。</p></div><button class="btn secondary" :disabled="busy" @click="load">刷新诊断</button></div>
  <p v-if="error" class="notice warn space-bottom" role="alert">{{ error }}</p><p v-if="feedback" class="notice space-bottom" role="status">{{ feedback }}</p>
  <div class="split-70"><div class="section-stack">
    <section class="card"><div class="card-pad"><div class="card-header"><div><h2>模型与记录</h2><p>API Key 由服务端配置，不在前端展示。</p></div><AppIcon name="server" /></div></div>
      <form @submit.prevent="save"><div class="settings-form"><label>模型接口<input v-model="endpoint" type="url" required :disabled="busy" /></label><label>默认模型<input v-model="model" required :disabled="busy" /></label><label>历史保留天数<input v-model.number="retention" type="number" min="1" max="3650" required :disabled="busy" /></label><p class="field-hint">模型须支持原生工具调用与结构化输出。更新设置不会修改已经冻结的任务配置。</p></div><div class="settings-actions"><p class="muted small">更改保存在本机服务端。</p><button class="btn primary" :disabled="busy || !state">{{ busy ? '处理中…' : '保存设置' }}</button></div></form>
    </section>
    <section class="card card-pad"><div class="card-header"><h2>外观偏好</h2><AppIcon name="settings" /></div><div class="grid two"><label class="field-label">主题<select v-model="theme" class="select"><option value="light">浅色</option><option value="dark">深色</option></select></label><label class="field-label">信息密度<select v-model="density" class="select"><option value="comfortable">舒适</option><option value="compact">紧凑</option></select></label></div><p class="field-hint">仅保存在当前浏览器，不改变审查结果或运行保护上限。</p></section>
  </div><aside class="plan-aside"><section class="card card-pad"><div class="card-header"><h2>服务状态</h2><AppIcon name="shield" /></div><p v-if="!state" class="muted" role="status">{{ busy ? '正在读取诊断…' : '诊断尚不可用' }}</p>
    <template v-else><div v-for="item in [['数据库',state.database_schema_current?'当前结构':'需要执行迁移'],['审查 Worker',state.worker_status==='running'?'运行中':'空闲'],['模型凭据',state.configured_secrets.llm_provider?'已配置':'未配置'],['证据目录',state.artifact_directory_writable?'可写':'不可写'],['追踪上传',state.langsmith_enabled?'已启用':'未启用']]" :key="item[0]" class="aside-stat"><span class="muted">{{ item[0] }}</span><strong>{{ item[1] }}</strong></div></template>
    <p class="notice small space-top">RepoGuardian 不执行目标仓库代码、测试或构建。发布权限与只读审查凭据分别配置。</p>
  </section></aside></div>
</template>
