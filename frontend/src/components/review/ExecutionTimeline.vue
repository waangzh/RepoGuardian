<script setup lang="ts">
import { computed } from "vue";
import type { AgentEvent, ReviewMode, TaskStatus, TaskStep, TestRunResult } from "../../types/review";
import PanelHeader from "../common/PanelHeader.vue";
import StatusBadge from "../common/StatusBadge.vue";

const props = defineProps<{
  steps: TaskStep[];
  taskStatus?: TaskStatus;
  mode: ReviewMode;
  events: AgentEvent[];
  staticResults: TestRunResult[];
  testResults: TestRunResult[];
}>();

interface FlowItem {
  title: string;
  description: string;
  aliases: string[];
  optional?: "patch" | "validation";
}

const flow: FlowItem[] = [
  { title: "获取 PR", description: "拉取元数据与目标分支", aliases: ["intake", "repo_prepare"] },
  { title: "解析 Diff", description: "解析变更并建立仓库索引", aliases: ["diff_parse", "repo_index"] },
  { title: "规划审查单元", description: "按文件、符号和风险拆分", aliases: ["review_plan"] },
  { title: "Unit 并发审查", description: "独立分析各审查单元", aliases: ["review_units"] },
  { title: "证据解析", description: "定位问题证据与代码上下文", aliases: ["resolve_evidence"] },
  { title: "问题验证", description: "核验问题有效性与影响范围", aliases: ["issue_policy", "issue_verifier"] },
  { title: "问题去重", description: "合并重复发现并统一结论", aliases: ["issue_deduplication"] },
  { title: "生成候选补丁", description: "为符合条件的问题生成修复", aliases: ["repair_policy", "generate_patch", "candidate_check"], optional: "patch" },
  { title: "补丁验证", description: "使用受控后端验证候选补丁", aliases: ["optional_validation"], optional: "validation" },
  { title: "生成报告", description: "汇总结论与 Markdown 报告", aliases: ["report", "complete"] },
];

const terminal = computed(() => ["completed", "completed_with_warnings", "failed", "cancelled"].includes(props.taskStatus || ""));

const items = computed(() => flow.map((item) => {
  const matches = props.steps.filter((step) => item.aliases.some((alias) => step.name.toLowerCase().includes(alias)));
  let status = "pending";
  if (matches.some((step) => step.status === "failed")) status = "failed";
  else if (matches.some((step) => step.status === "running")) status = "running";
  else if (matches.length && matches.every((step) => step.status === "completed")) status = "completed";
  else if (!matches.length && terminal.value && item.optional === "patch" && props.mode === "review") status = "skipped";
  else if (!matches.length && terminal.value && item.optional === "validation" && props.mode !== "review_suggest_and_validate") status = "skipped";
  return { ...item, status };
}));
</script>

<template>
  <section class="panel execution-panel">
    <PanelHeader icon="settings" title="Agent 执行进度" subtitle="可追溯审查流程">
      <StatusBadge :status="taskStatus || 'pending'" />
    </PanelHeader>
    <ol class="execution-flow">
      <li v-for="(item, index) in items" :key="item.title" :data-status="item.status">
        <span class="execution-flow__index">{{ index + 1 }}</span>
        <div><strong>{{ item.title }}</strong><p>{{ item.description }}</p></div>
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
