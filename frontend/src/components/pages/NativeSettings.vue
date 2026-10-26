<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { getDiagnostics, updateSystemSettings } from '../../api/client';
import type { SystemDiagnostics } from '../../types/operations';
const state = ref<SystemDiagnostics | null>(null);
const error = ref('');
const busy = ref(false);
const endpoint = ref('');
const model = ref('');
const retention = ref(30);
async function load() {
  busy.value = true;
  try { state.value = await getDiagnostics(); endpoint.value = state.value.model_base_url; model.value = state.value.default_model; retention.value = state.value.retention_days; }
  catch (e) { error.value = e instanceof Error ? e.message : '读取设置失败'; }
  finally { busy.value = false; }
}
async function save() {
  busy.value = true; error.value = '';
  try { state.value = await updateSystemSettings({model_base_url:endpoint.value,model_name:model.value,retention_days:retention.value}); }
  catch (e) { error.value = e instanceof Error ? e.message : '保存失败'; }
  finally { busy.value = false; }
}
onMounted(load);
</script>
<template>
  <section class="native-panel"><h2>模型与本地配置</h2><p>模型必须支持原生工具调用。API Key 仅由服务端配置，不会显示在页面。</p>
    <form @submit.prevent="save" class="native-form"><label>模型接口<input v-model="endpoint" type="url" required :disabled="busy" /></label><label>默认模型<input v-model="model" required :disabled="busy" /></label><label>历史保留天数<input v-model.number="retention" type="number" min="1" max="3650" required :disabled="busy" /></label><button class="button button--primary" :disabled="busy">{{ busy ? '处理中…' : '保存设置' }}</button></form>
    <p v-if="error" role="alert" class="native-error">{{ error }}</p>
    <dl v-if="state" class="native-facts"><div><dt>数据库</dt><dd>{{ state.database_schema_current ? '当前结构' : '需要执行迁移' }}</dd></div><div><dt>审查 Worker</dt><dd>{{ state.worker_status === 'running' ? '运行中' : '空闲' }}</dd></div><div><dt>模型凭据</dt><dd>{{ state.configured_secrets.llm_provider ? '已配置' : '未配置' }}</dd></div><div><dt>只读边界</dt><dd>不执行目标代码、测试或构建</dd></div></dl>
  </section>
</template>
