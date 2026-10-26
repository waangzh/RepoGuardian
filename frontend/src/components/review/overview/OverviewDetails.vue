<script setup lang="ts">
import { computed } from "vue";
import type { ReviewSummary } from "../../../types/publication";
import type { ReviewTask } from "../../../types/review";
import type { ReviewSection } from "../../../types/workspace";
import type { ReviewSituation } from "../../../utils/reviewSituation";
import type { TaskRuntimeSummary } from "../../../utils/taskRuntimePresentation";
import { overviewFacts, overviewNextSteps } from "../../../utils/overviewPresentation";
import AppIcon from "../../common/AppIcon.vue";
import PrDiscussionContext from "../PrDiscussionContext.vue";

const props = defineProps<{
  plan: ReviewSummary | null; runtime: TaskRuntimeSummary | null; warnings: string[];
  task: ReviewTask; situation: ReviewSituation | null; execution: string;
}>();
const emit = defineEmits<{ navigate: [section: ReviewSection] }>();
const facts = computed(() => overviewFacts(props.plan, props.runtime));
const extraWarnings = computed(() => [...new Set(props.warnings)]
  .filter(warning => !facts.value.boundaries.some(row => row.description === warning)));
const next = computed(() => overviewNextSteps(props.situation));
const head = computed(() => props.plan?.snapshot.head_sha || props.task.snapshot?.head_sha || props.task.pr?.head.sha);
const depth = computed(() => props.task.confirmed_plan
  ? ({ quick: "快速初筛", standard: "标准审查", deep: "深度核验" })[props.task.confirmed_plan.depth] : null);
</script>

<template>
  <section class="ov-supplement" aria-label="补充信息">
    <details class="ov-panel ov-disclosure">
      <summary>检查范围与限制</summary>
      <div class="ov-boundary">
        <h2>结论依据与可信边界</h2>
        <div class="ov-boundary__rows">
          <button v-for="row in facts.boundaries" :key="row.title" type="button" class="ov-boundary__row" :aria-label="`查看${row.title}详情`" @click="emit('navigate', row.target)">
            <span class="ov-state-icon" :data-tone="row.tone"><AppIcon :name="row.icon" :size="18" /></span>
            <span class="ov-boundary__copy"><strong>{{ row.title }} <em class="ov-tag" :data-tone="row.tone">{{ row.value }}</em></strong><small v-if="row.description">{{ row.description }}</small></span>
            <AppIcon name="arrow-right" :size="16" />
          </button>
        </div>
        <ul v-if="extraWarnings.length" class="ov-limitations"><li v-for="warning in extraWarnings" :key="warning">{{ warning }}</li></ul>
      </div>
    </details>
    <details class="ov-panel ov-disclosure">
      <summary>历史讨论</summary>
      <PrDiscussionContext :review-id="task.id" :stats="plan?.digest.discussion_stats" />
    </details>
    <details class="ov-panel ov-disclosure ov-step-details">
      <summary>处理步骤与审查快照</summary>
      <div class="ov-snapshot">
        <h2>本轮审查快照</h2>
        <dl>
          <div><dt>审查模式</dt><dd>{{ depth ? `${depth} · ` : "" }}只读</dd></div>
          <div><dt>执行状态</dt><dd>{{ execution }}</dd></div>
          <div><dt>变更文件</dt><dd>{{ task.changed_files.length }} 个变更文件</dd></div>
          <div><dt>Head SHA</dt><dd><code>{{ head?.slice(0, 12) || "未知" }}</code></dd></div>
        </dl>
        <p><AppIcon name="info" :size="14" /><span>结论只适用于记录的 PR 快照。页面加载不代表已自动核对 GitHub 最新 Head。</span></p>
        <ol class="ov-steps"><li v-for="([title, description], index) in next.steps" :key="title"><span>{{ index + 1 }}</span><div><strong>{{ title }}</strong><small>{{ description }}</small></div></li></ol>
      </div>
    </details>
    <button type="button" class="ov-link" @click="emit('navigate', 'activity')"><AppIcon name="activity" :size="15" />模型用量与技术审计<AppIcon name="arrow-right" :size="15" /></button>
  </section>
</template>
