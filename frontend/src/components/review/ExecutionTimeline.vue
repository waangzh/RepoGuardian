<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import type { AgentEvent, TaskStatus, TaskStep, TestRunResult } from "../../types/review";
import PanelHeader from "../common/PanelHeader.vue";
import StatusBadge from "../common/StatusBadge.vue";

const props = defineProps<{
  steps: TaskStep[];
  taskStatus?: TaskStatus;
  events: AgentEvent[];
  staticResults: TestRunResult[];
  testResults: TestRunResult[];
}>();

interface FlowItem {
  title: string;
  description: string;
  aliases: string[];
}

interface FlowDisplayItem extends FlowItem {
  status: TaskStep["status"];
  activeStep: TaskStep;
}

const flow: FlowItem[] = [
  { title: "获取 PR", description: "拉取元数据与目标分支", aliases: ["queued", "intake", "repo_prepare"] },
  { title: "解析 Diff", description: "解析变更、建立索引并识别项目", aliases: ["diff_parse", "repo_index", "project_detection"] },
  { title: "规划审查单元", description: "按文件、符号和风险拆分", aliases: ["review_plan"] },
  { title: "Unit 并发审查", description: "独立分析各审查单元", aliases: ["review_units"] },
  { title: "证据解析", description: "定位问题证据与代码上下文", aliases: ["resolve_evidence"] },
  { title: "问题验证", description: "核验问题有效性与影响范围", aliases: ["issue_policy", "issue_verifier"] },
  { title: "问题去重", description: "合并重复发现并统一结论", aliases: ["issue_deduplication"] },
  { title: "固化验证结论", description: "固化 Head 基线与自动修复边界", aliases: ["verification"] },
  { title: "评估修复策略", description: "判断问题是否满足候选修复条件", aliases: ["repair_policy"] },
  { title: "生成候选补丁", description: "为符合条件的问题生成修复", aliases: ["generate_patch", "patch_generate", "candidate_check", "mark_unverified", "patch_finalize"] },
  { title: "补丁验证", description: "使用受控后端验证候选补丁", aliases: ["validation", "optional_validation", "patched_validation", "repair_assessment", "repair_accept", "repair_abandon"] },
  { title: "生成报告", description: "汇总结论与 Markdown 报告", aliases: ["report", "complete"] },
];

const now = ref(Date.now());
let elapsedTimer: number | undefined;

onMounted(() => {
  elapsedTimer = window.setInterval(() => { now.value = Date.now(); }, 1000);
});

onBeforeUnmount(() => {
  if (elapsedTimer !== undefined) window.clearInterval(elapsedTimer);
});

const items = computed<FlowDisplayItem[]>(() => flow.flatMap((item) => {
  const matches = props.steps.filter((step) => item.aliases.some((alias) => step.name.toLowerCase().includes(alias)));
  if (!matches.length) return [];
  let status: TaskStep["status"] = "pending";
  if (matches.some((step) => step.status === "failed")) status = "failed";
  else if (matches.some((step) => step.status === "running")) status = "running";
  else if (matches.length && matches.every((step) => step.status === "completed")) status = "completed";
  const activeStep = [...matches].reverse().find((step) => step.status === "running") || matches[matches.length - 1];
  return [{ ...item, status, activeStep }];
}));

function formatElapsed(startedAt?: string | null): string {
  if (!startedAt) return "";
  const started = new Date(startedAt).getTime();
  if (!Number.isFinite(started)) return "";
  const seconds = Math.max(0, Math.floor((now.value - started) / 1000));
  return seconds < 60
    ? `已用时 ${seconds} 秒`
    : `已用时 ${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

function progressDetail(step: TaskStep): string {
  const progress = step.progress;
  if (!progress) return "";
  const parts: string[] = [];
  if (progress.current !== null && progress.current !== undefined && progress.total) {
    parts.push(`${progress.current.toLocaleString()} / ${progress.total.toLocaleString()}`);
  }
  if (progress.detail) parts.push(progress.detail);
  return parts.join(" · ");
}

function progressScale(percent?: number | null): number {
  return Math.max(0, Math.min(percent ?? 0, 100)) / 100;
}
</script>

<template>
  <section class="panel execution-panel">
    <PanelHeader icon="settings" title="审查执行进度" subtitle="准备、Agent 审查与验证流程">
      <StatusBadge :status="taskStatus || 'pending'" />
    </PanelHeader>
    <ol class="execution-flow">
      <li v-for="(item, index) in items" :key="item.title" :data-status="item.status">
        <span class="execution-flow__index">{{ index + 1 }}</span>
        <div class="execution-flow__content">
          <strong>{{ item.title }}</strong>
          <p>{{ item.activeStep.message || item.description }}</p>
          <div
            v-if="item.status === 'running' && item.activeStep.progress"
            class="step-progress"
            aria-live="polite"
          >
            <div class="step-progress__meta">
              <span>{{ formatElapsed(item.activeStep.started_at) }}</span>
              <b v-if="item.activeStep.progress.percent !== null && item.activeStep.progress.percent !== undefined">
                {{ item.activeStep.progress.percent }}%
              </b>
              <b v-else>进行中</b>
            </div>
            <div
              class="step-progress__track"
              :class="{ 'is-indeterminate': item.activeStep.progress.percent === null || item.activeStep.progress.percent === undefined }"
              role="progressbar"
              :aria-label="item.activeStep.message || item.title"
              aria-valuemin="0"
              aria-valuemax="100"
              :aria-valuenow="item.activeStep.progress.percent ?? undefined"
            >
              <span :style="{ transform: `scaleX(${progressScale(item.activeStep.progress.percent)})` }" />
            </div>
            <small v-if="progressDetail(item.activeStep)">{{ progressDetail(item.activeStep) }}</small>
          </div>
        </div>
        <StatusBadge :status="item.status" />
      </li>
    </ol>
    <details v-if="events.length || staticResults.length || testResults.length" class="activity-log">
      <summary>查看执行记录（{{ events.length + staticResults.length + testResults.length }}）</summary>
      <div v-for="event in events" :key="`${event.created_at}:${event.action}`" class="activity-log__row">
        <strong>{{ event.action }}</strong><StatusBadge :status="event.status" /><p>{{ event.reason }}</p>
      </div>
      <div v-for="result in [...staticResults, ...testResults]" :key="`${result.tool}:${result.command}`" class="activity-log__row">
        <strong>{{ result.command }}</strong><StatusBadge :status="result.passed ? 'passed' : 'failed'" /><p>exit {{ result.exit_code }} · {{ result.duration.toFixed(2) }}s</p>
      </div>
    </details>
  </section>
</template>
