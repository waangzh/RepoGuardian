<script setup lang="ts">
import { computed } from "vue";
import type { ReviewSituation, SituationActionKey } from "../../../utils/reviewSituation";
import type { ReviewSummary } from "../../../types/publication";
import type { ReviewTask } from "../../../types/review";
import type { ReviewSection } from "../../../types/workspace";
import CopyCommentButton from "../publication/CopyCommentButton.vue";
import AppIcon from "../../common/AppIcon.vue";

const props = defineProps<{ situation: ReviewSituation | null; plan: ReviewSummary | null; task: ReviewTask; summaryError?: boolean; summaryLoading?: boolean }>();
const emit = defineEmits<{
  action: [key: SituationActionKey];
  navigate: [section: ReviewSection];
  snapshot: [status: "current" | "stale" | "unknown"];
  reload: [];
}>();
const hasReportAction = computed(() => props.situation?.actions.some(action => action.key === "view_report"));
</script>

<template>
  <div class="ov-next" aria-label="下一步操作">
    <div class="ov-actions">
      <button v-if="situation?.actions[0]" type="button" class="btn primary ov-main-action" @click="emit('action', situation.actions[0].key)">{{ situation.actions[0].label }}<AppIcon name="arrow-right" :size="16" /></button>
      <button v-else-if="summaryError || (!summaryLoading && !plan)" type="button" class="btn primary ov-main-action" :disabled="summaryLoading" @click="emit('reload')">重试加载摘要<AppIcon name="arrow-right" :size="16" /></button>
      <button v-else type="button" class="btn primary ov-main-action" @click="emit('navigate', 'activity')">查看技术记录 <AppIcon name="arrow-right" :size="16" /></button>
        <template v-if="situation?.copy && plan">
          <CopyCommentButton :review-id="task.id" :reviewed-head-sha="plan.snapshot.head_sha" :markdown="plan.review_comment_markdown || ''" :label="situation.copy.label" :primary="false" :partial="situation.copy.partial" @snapshot="emit('snapshot', $event)" @rereview="emit('action', 'rereview')" />
        </template>
    </div>
    <div class="ov-other-actions">
      <button v-for="action in situation?.actions.slice(1) || []" :key="action.key" type="button" class="ov-link" @click="emit('action', action.key)">{{ action.label }}<AppIcon :name="action.icon" :size="15" /></button>
      <button v-if="!hasReportAction" type="button" class="ov-link" @click="emit('navigate', 'report')">阅读详细审查报告<AppIcon name="report" :size="16" /></button>
    </div>
  </div>
</template>
