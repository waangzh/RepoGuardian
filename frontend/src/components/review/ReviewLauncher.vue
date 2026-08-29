<script setup lang="ts">
import { computed } from "vue";
import type { ProviderModelInfo } from "../../types/operations";
import type { ReviewMode, ValidationBackend } from "../../types/review";
import AppIcon from "../common/AppIcon.vue";
import AppSelect from "../common/AppSelect.vue";

const props = defineProps<{
  prUrl: string;
  model: string;
  mode: ReviewMode;
  generatePatches: boolean;
  validationBackend: ValidationBackend;
  previewing: boolean;
  submitting: boolean;
  active: boolean;
  cancelling: boolean;
  error: string | null;
  models: ProviderModelInfo[];
  defaultModel: string;
  modelsLoading: boolean;
  modelsError: string | null;
}>();

const emit = defineEmits<{
  "update:prUrl": [value: string];
  "update:model": [value: string];
  "update:mode": [value: ReviewMode];
  "update:generatePatches": [value: boolean];
  "update:validationBackend": [value: ValidationBackend];
  preview: [];
  submit: [];
  cancel: [];
}>();

const prUrlModel = computed({ get: () => props.prUrl, set: (value) => emit("update:prUrl", value) });
const modelModel = computed({ get: () => props.model, set: (value) => emit("update:model", value) });
const modeModel = computed({ get: () => props.mode, set: (value) => emit("update:mode", value) });
const patchModel = computed({ get: () => props.generatePatches, set: (value) => emit("update:generatePatches", value) });
const backendModel = computed({ get: () => props.validationBackend, set: (value) => emit("update:validationBackend", value) });

const modelOptions = computed(() => [
  {
    value: "",
    label: props.modelsLoading
      ? "正在查询可用模型…"
      : props.defaultModel
        ? `后端默认：${props.defaultModel}`
        : "使用后端默认模型",
    description: props.modelsLoading ? "正在从模型服务同步列表" : "跟随后端环境配置",
  },
  ...props.models.map((item) => ({
    value: item.id,
    label: item.id,
    description: item.owned_by ? `提供方 · ${item.owned_by}` : "可用模型",
  })),
]);

const modeOptions = [
  { value: "review", label: "严格只读审查", description: "基于静态证据审查；动态验证由 Project CI 异步完成" },
];

const backendOptions = [
  { value: "none", label: "不执行验证", description: "仅生成候选补丁" },
  { value: "user_runner", label: "用户 Runner", description: "交由用户侧执行环境验证" },
  { value: "project_ci", label: "项目 CI", description: "使用项目现有 CI 流程" },
  { value: "gvisor", label: "gVisor（已废弃）", description: "不可执行兼容占位；请使用 Project CI" },
];

function handleSubmit() {
  if (props.submitting || props.previewing || props.active || !props.prUrl.trim()) return;
  emit("submit");
}

function handlePreview() {
  if (props.previewing || props.submitting || props.active || !props.prUrl.trim()) return;
  emit("preview");
}
</script>

<template>
  <aside class="review-launcher">
    <form class="launcher-form" @submit.prevent="handleSubmit">
      <div class="launcher-heading">
        <span class="launcher-heading__icon"><AppIcon name="play" :size="18" /></span>
        <div><h2>启动新审查</h2><p>配置审查任务并启动 AI 评审流程</p></div>
      </div>

      <label class="field">
        <span>GitHub PR URL</span>
        <div class="input-with-icon">
          <span><AppIcon name="branch" :size="14" /></span>
          <input v-model="prUrlModel" type="url" placeholder="https://github.com/owner/repo/pull/123" :disabled="active" required />
        </div>
      </label>

      <div class="field">
        <span>模型 / Model</span>
        <AppSelect
          v-model="modelModel"
          :options="modelOptions"
          :disabled="active || modelsLoading"
          aria-label="模型"
          aria-describedby="model-help"
        />
        <small id="model-help" :class="{ 'field-help--error': modelsError }">
          {{ modelsError || (models.length ? `已查询到 ${models.length} 个可用模型，可手动选择` : "将使用后端配置的默认模型") }}
        </small>
      </div>

      <div class="field">
        <span>审查模式</span>
        <AppSelect v-model="modeModel" :options="modeOptions" :disabled="active" aria-label="审查模式" />
      </div>

      <template v-if="mode === 'review_suggest_and_validate'">
        <div class="field">
          <span>验证后端</span>
          <AppSelect v-model="backendModel" :options="backendOptions" :disabled="active" aria-label="验证后端" />
        </div>
        <label class="field">
          <span>Validation Profile</span>
          <input value="由所选验证后端与项目自动确定" disabled aria-describedby="profile-help" />
          <small id="profile-help">当前 API 不支持手动指定 Profile</small>
        </label>
      </template>

      <label v-if="mode !== 'review'" class="switch-row">
        <span><strong>生成候选补丁</strong><small>发现问题时生成修复建议</small></span>
        <input v-model="patchModel" type="checkbox" role="switch" :disabled="active" />
      </label>

      <div class="launcher-actions">
        <button v-if="active" type="button" class="button button--secondary" :disabled="cancelling" @click="$emit('cancel')">
          {{ cancelling ? "取消中…" : "取消审查" }}
        </button>
        <button v-else type="button" class="button button--secondary" :disabled="previewing || submitting || !prUrl" @click="handlePreview">
          {{ previewing ? "分析中…" : "Preview" }}
        </button>
        <button type="submit" class="button button--primary" :disabled="submitting || previewing || active || !prUrl" :aria-busy="submitting || previewing || active">
          {{ submitting ? "提交中…" : active ? "审查进行中…" : "开始审查" }}
        </button>
      </div>

      <p class="safety-note"><AppIcon name="info" :size="16" />Preview 仅执行 PR 拉取、Diff 解析和确定性规划，不调用模型，也不运行目标仓库代码。</p>
      <p v-if="error" class="form-error" role="alert">{{ error }}</p>
    </form>

  </aside>
</template>
