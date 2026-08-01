<script setup lang="ts">
import { computed } from "vue";
import type { ReviewMode, ReviewPreviewResponse, ValidationBackend } from "../../types/review";

const props = defineProps<{
  prUrl: string;
  model: string;
  mode: ReviewMode;
  generatePatches: boolean;
  validationBackend: ValidationBackend;
  previewing: boolean;
  submitting: boolean;
  error: string | null;
  preview: ReviewPreviewResponse | null;
}>();

const emit = defineEmits<{
  "update:prUrl": [value: string];
  "update:model": [value: string];
  "update:mode": [value: ReviewMode];
  "update:generatePatches": [value: boolean];
  "update:validationBackend": [value: ValidationBackend];
  preview: [];
  submit: [];
}>();

const prUrlModel = computed({ get: () => props.prUrl, set: (value) => emit("update:prUrl", value) });
const modelModel = computed({ get: () => props.model, set: (value) => emit("update:model", value) });
const modeModel = computed({ get: () => props.mode, set: (value) => emit("update:mode", value) });
const patchModel = computed({ get: () => props.generatePatches, set: (value) => emit("update:generatePatches", value) });
const backendModel = computed({ get: () => props.validationBackend, set: (value) => emit("update:validationBackend", value) });
</script>

<template>
  <aside class="review-launcher">
    <form class="launcher-form" @submit.prevent="$emit('submit')">
      <div class="launcher-heading">
        <span class="launcher-heading__icon" aria-hidden="true">↗</span>
        <div><h2>启动新审查</h2><p>配置审查任务并启动 AI 评审流程</p></div>
      </div>

      <label class="field">
        <span>GitHub PR URL</span>
        <div class="input-with-icon">
          <span aria-hidden="true">◉</span>
          <input v-model="prUrlModel" type="url" placeholder="https://github.com/owner/repo/pull/123" required />
        </div>
      </label>

      <label class="field">
        <span>模型 / Model</span>
        <input v-model="modelModel" type="text" placeholder="使用后端默认模型" />
      </label>

      <label class="field">
        <span>审查模式</span>
        <select v-model="modeModel">
          <option value="review">只读审查（不生成补丁）</option>
          <option value="review_and_suggest">审查 + 候选补丁</option>
          <option value="review_suggest_and_validate">审查 + 补丁 + 验证</option>
        </select>
      </label>

      <template v-if="mode === 'review_suggest_and_validate'">
        <label class="field">
          <span>验证后端</span>
          <select v-model="backendModel">
            <option value="none">不执行验证</option>
            <option value="user_runner">用户 Runner</option>
            <option value="project_ci">项目 CI</option>
            <option value="gvisor">gVisor</option>
          </select>
        </label>
        <label class="field">
          <span>Validation Profile</span>
          <input value="由所选验证后端与项目自动确定" disabled aria-describedby="profile-help" />
          <small id="profile-help">当前 API 不支持手动指定 Profile</small>
        </label>
      </template>

      <label v-if="mode !== 'review'" class="switch-row">
        <span><strong>生成候选补丁</strong><small>发现问题时生成修复建议</small></span>
        <input v-model="patchModel" type="checkbox" role="switch" />
      </label>

      <div class="launcher-actions">
        <button type="button" class="button button--secondary" :disabled="previewing || !prUrl" @click="$emit('preview')">
          {{ previewing ? "分析中…" : "Preview" }}
        </button>
        <button type="submit" class="button button--primary" :disabled="submitting || !prUrl">
          {{ submitting ? "启动中…" : "开始审查" }}
        </button>
      </div>

      <p class="safety-note"><span aria-hidden="true">◆</span>Preview 仅执行 PR 拉取、Diff 解析和确定性规划，不调用模型，也不运行目标仓库代码。</p>
      <p v-if="error" class="form-error" role="alert">{{ error }}</p>
    </form>

    <section v-if="preview" class="preview-summary" aria-label="审查 Preview">
      <header><strong>确定性 Preview</strong><span>{{ preview.review_units.length }} Units</span></header>
      <div class="preview-summary__metrics">
        <span><strong>{{ preview.included_file_count }}/{{ preview.changed_file_count }}</strong>审查文件</span>
        <span><strong>{{ preview.estimated_model_calls }}</strong>模型调用</span>
        <span><strong>{{ preview.estimated_tokens.toLocaleString() }}</strong>预计 Token</span>
      </div>
      <p>候选补丁 {{ preview.patch_generation_enabled ? "已开启" : "未开启" }} · 验证后端 {{ preview.validation_backend.name }}</p>
      <p v-if="preview.validation_backend.unavailable_reason" class="form-error">{{ preview.validation_backend.unavailable_reason }}</p>
      <details v-if="preview.review_units.length || preview.excluded_files.length">
        <summary>查看规划详情</summary>
        <p v-for="unit in preview.review_units" :key="unit.id" class="preview-summary__row">
          <code>{{ unit.primary_files[0] }}</code><span>{{ unit.complexity }} · {{ unit.estimated_tokens.toLocaleString() }} tokens</span>
        </p>
        <p v-for="file in preview.excluded_files" :key="file.file_path" class="preview-summary__row">
          <code>{{ file.file_path }}</code><span>已排除 · {{ file.reason }}</span>
        </p>
      </details>
    </section>
  </aside>
</template>
