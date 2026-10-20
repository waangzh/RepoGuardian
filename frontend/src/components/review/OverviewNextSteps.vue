<script setup lang="ts">
import { computed } from "vue";
import type { ReviewSituation, SituationActionKey } from "../../utils/reviewSituation";
import type { ReviewSummary } from "../../types/publication";
import type { ReviewTask } from "../../types/review";
import type { ReviewSection } from "../../types/workspace";
import { overviewNextSteps } from "../../utils/overviewPresentation";
import CopyCommentButton from "./CopyCommentButton.vue";
import AppIcon from "../common/AppIcon.vue";

const props = defineProps<{ situation: ReviewSituation | null; plan: ReviewSummary | null; task: ReviewTask; execution: string; summaryError?: boolean; summaryLoading?: boolean }>();
const emit = defineEmits<{
  action: [key: SituationActionKey];
  navigate: [section: ReviewSection];
  snapshot: [status: "current" | "stale" | "unknown"];
  reload: [];
}>();
const next = computed(() => overviewNextSteps(props.situation));
const hasReportAction = computed(() => props.situation?.actions.some(action => action.key === "view_report"));
const head = computed(() => props.plan?.snapshot.head_sha || props.task.snapshot?.head_sha || props.task.pr?.head.sha);
const depth = computed(() => props.task.confirmed_plan
  ? ({ quick: "快速初筛", standard: "标准审查", deep: "深度核验" })[props.task.confirmed_plan.depth] : null);
</script>

<template>
  <aside class="ov-side" aria-label="建议操作与审查快照">
    <section class="ov-panel ov-next">
      <span class="ov-overline">RECOMMENDED NEXT</span>
      <h2>{{ next.title }}</h2><p>{{ next.description }}</p>
      <button v-if="situation?.actions[0]" type="button" class="btn ov-main-action" :class="situation.key === 'clean' ? 'secondary' : 'primary'" @click="emit('action', situation.actions[0].key)">{{ situation.actions[0].label }}<AppIcon name="arrow-right" :size="16" /></button>
      <button v-else-if="summaryError || (!summaryLoading && !plan)" type="button" class="btn primary ov-main-action" :disabled="summaryLoading" @click="emit('reload')">重试加载摘要<AppIcon name="arrow-right" :size="16" /></button>
      <button v-else type="button" class="btn primary ov-main-action" @click="emit('navigate', 'activity')">查看技术记录 <AppIcon name="arrow-right" :size="16" /></button>
      <div v-if="situation && situation.actions.length > 1" class="ov-other-actions"><button v-for="action in situation.actions.slice(1)" :key="action.key" type="button" class="ov-link" @click="emit('action', action.key)">{{ action.label }}<AppIcon :name="action.icon" :size="15" /></button></div>
      <div class="ov-next__delivery">
        <button v-if="!hasReportAction" type="button" class="btn secondary" @click="emit('navigate', 'report')"><AppIcon name="report" :size="16" />阅读详细审查报告</button>
        <template v-if="situation?.copy && plan">
          <CopyCommentButton :review-id="task.id" :reviewed-head-sha="plan.snapshot.head_sha" :markdown="plan.review_comment_markdown || ''" :label="situation.copy.label" :primary="situation.key === 'clean'" :partial="situation.copy.partial" @snapshot="emit('snapshot', $event)" @rereview="emit('action', 'rereview')" />
          <p>{{ situation.copy.partial ? "仅复制用户停止或执行失败前保留的部分审查记录。" : "复制前会核对 PR 当前 Head，内容仅适用于本次快照。" }}</p>
        </template>
      </div>
      <details class="ov-step-details"><summary>查看处理步骤</summary><ol class="ov-steps"><li v-for="([title, description], index) in next.steps" :key="title"><span>{{ index + 1 }}</span><div><strong>{{ title }}</strong><small>{{ description }}</small></div></li></ol></details>
    </section>
    <section class="ov-panel ov-snapshot" aria-labelledby="overview-snapshot-title">
      <h2 id="overview-snapshot-title">本轮审查快照</h2>
      <dl>
        <div><dt>审查模式</dt><dd>{{ depth ? `${depth} · ` : "" }}只读</dd></div>
        <div><dt>执行状态</dt><dd>{{ execution }}</dd></div>
        <div><dt>变更文件</dt><dd>{{ task.changed_files.length }} 个变更文件</dd></div>
        <div><dt>Head SHA</dt><dd><code>{{ head?.slice(0, 12) || "未知" }}</code></dd></div>
      </dl>
      <p><AppIcon name="info" :size="14" /><span>结论只适用于记录的 PR 快照。页面加载不代表已自动核对 GitHub 最新 Head。</span></p>
      <button type="button" class="ov-link" @click="emit('navigate', 'activity')"><AppIcon name="activity" :size="15" />查看技术运行记录 <AppIcon name="arrow-right" :size="15" /></button>
    </section>
  </aside>
</template>
