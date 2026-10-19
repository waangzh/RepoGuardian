<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { ReviewTask } from "../../types/review";
import type { ReviewSummary } from "../../types/publication";
import type { RuntimeFindingRow, TaskRuntimeSummary } from "../../utils/taskRuntimePresentation";
import type { ReviewSection } from "../../types/workspace";
import type { ReviewSituation, SituationActionKey } from "../../utils/reviewSituation";
import { reviewSituation } from "../../utils/reviewSituation";
import ReviewOutcomePanel from "../review/ReviewOutcomePanel.vue";
import PrChangeSummary from "../review/PrChangeSummary.vue";
import CopyCommentButton from "../review/CopyCommentButton.vue";
import AppIcon from "../common/AppIcon.vue";
const props = defineProps<{ task: ReviewTask; report?: string | null; summaryPlan: ReviewSummary | null; summary: TaskRuntimeSummary | null; findings: RuntimeFindingRow[]; warnings: string[]; terminal: boolean }>();
const emit = defineEmits<{ navigate: [section: ReviewSection]; finding: [key: string]; create: [] }>();

// “PR 已有新提交”只有复制前快照检查才能确认；页面加载不额外访问 GitHub。
// 复制检查返回 stale 时切换到旧版本情景，用户重新审查前不会误用原结论。
const stale = ref(false);
watch(() => props.task.id, () => { stale.value = false; });

const situation = computed<ReviewSituation | null>(() => reviewSituation(props.summaryPlan, { stale: stale.value }));
const examined = computed(() => props.summaryPlan?.outcome.examined_goals ?? props.summary?.scope.examined ?? null);
const total = computed(() => props.summaryPlan?.outcome.total_goals ?? props.summary?.scope.total ?? null);
const dynamicValidation = computed(() => props.summaryPlan?.outcome.dynamic_validation
  || props.summaryPlan?.digest.validation_state || "unknown");
const validationLabel = computed(() => ({ not_executed: "未执行目标仓库代码、测试或构建",
  passed: "已有当前 Head 通过结果", failed: "已有当前 Head 失败结果",
  independent_results: "已有独立补丁结果，当前 Head 未知", unknown: "动态验证状态未知" })[dynamicValidation.value] || "动态验证状态未知");
const validations = computed(() => props.summaryPlan?.digest.validations || []);

// 建议行动：严格按情景矩阵给出主要操作；无风险时不出现“查看待确认风险”。
const primaryActions = computed(() => {
  const keys = situation.value?.actions.map(item => item.key) ?? [];
  const includeCopy = situation.value?.key === "clean" || situation.value?.key === "nonblocking";
  return { keys, includeCopy };
});
function runAction(key: SituationActionKey) {
  if (key === "view_blockers" || key === "view_unresolved" || key === "view_findings") emit("navigate", "findings");
  else if (key === "view_gaps") emit("navigate", "scope");
  else if (key === "view_validation" || key === "view_activity") emit("navigate", "activity");
  else if (key === "rereview") emit("create");
}
</script>
<template>
  <div class="section-stack overview-layers">
    <section class="overview-layer">
      <div class="layer-label">审查结果</div>
      <ReviewOutcomePanel v-if="summaryPlan" :plan="summaryPlan" :stale="stale" />
      <section v-else class="summary-banner"><div class="eyebrow">审查结果</div><h2>{{ !terminal ? '审查正在进行中' : '审查结果需要结合范围判断' }}</h2><p>结论尚未取得后端发布结果，请参考下方记录与限制。</p></section>
    </section>
    <section class="overview-layer">
      <div class="layer-label">核心发现</div>
      <div class="core-findings">
        <div class="core-finding"><strong>{{ situation?.blockingCount ?? '未知' }}</strong><span>阻断问题</span></div>
        <div class="core-finding"><strong>{{ situation?.unresolvedCount ?? '未知' }}</strong><span>待确认风险</span></div>
        <div class="core-finding"><strong>{{ examined ?? '未知' }}<small v-if="total !== null"> / {{ total }}</small></strong><span>目标已检查</span></div>
      </div>
    </section>
    <section class="overview-layer">
      <div class="layer-label">建议行动</div>
      <div class="overview-actions" :class="{ single: !primaryActions.includeCopy && primaryActions.keys.length <= 1 }">
        <div v-if="primaryActions.includeCopy" class="overview-action-cell">
          <CopyCommentButton v-if="summaryPlan" :review-id="task.id" :reviewed-head-sha="summaryPlan.snapshot.head_sha" :markdown="summaryPlan.review_comment_markdown || ''" @snapshot="stale = $event === 'stale'" />
          <button v-else type="button" class="btn secondary" @click="emit('navigate', 'report')">查看报告</button>
        </div>
        <button v-for="key in primaryActions.keys" :key="key" type="button" class="btn secondary overview-action" @click="runAction(key)">
          <AppIcon :name="(situation?.actions.find(item => item.key === key)?.icon) || 'arrow-right'" :size="16" />{{ situation?.actions.find(item => item.key === key)?.label }}
        </button>
      </div>
    </section>
    <section class="overview-layer">
      <div class="layer-label">更多信息</div>
      <div class="overview-more">
        <details class="overview-more__item">
          <summary>PR 变更理解</summary>
          <div class="overview-more__body"><PrChangeSummary :task="task" :report="report" :understanding="summaryPlan?.digest.change_understanding" :author-limitation="summaryPlan?.digest.author_context_limitation" /></div>
        </details>
        <details class="overview-more__item">
          <summary>审查范围与限制</summary>
          <div class="overview-more__body">
            <p class="small muted">只读静态审查：目标完成 {{ examined ?? '未知' }} / {{ total ?? '未知' }}；不表示代码正确，0 个问题也不代表所有业务路径已验证。</p>
            <p v-if="summaryPlan?.outcome.incomplete_investigations" class="small muted">仍有 {{ summaryPlan.outcome.incomplete_investigations }} 项调查未完成，相关结论需补充证据。</p>
            <ul v-if="warnings.length" class="rg-limitations"><li v-for="warning in warnings" :key="warning">{{ warning }}</li></ul>
            <button type="button" class="link" @click="emit('navigate', 'scope')">查看覆盖范围与局限 →</button>
          </div>
        </details>
        <details class="overview-more__item">
          <summary>动态验证</summary>
          <div class="overview-more__body">
            <p class="small muted">{{ validationLabel }}；动态结果与静态结论独立，不替代 CI 与人工审批。</p>
            <ul v-if="validations.length"><li v-for="item in validations" :key="item.id">{{ item.source }} / {{ item.stage }}：{{ item.status }}。{{ item.reason }}</li></ul>
          </div>
        </details>
        <details class="overview-more__item">
          <summary>技术记录</summary>
          <div class="overview-more__body">
            <p v-if="summary" class="small muted">执行状态：{{ summary.execution.status || '未知' }}；终止记录：{{ summary.execution.termination_reason || '未记录' }}；计量完整性：{{ summary.usage.integrity || '未知' }}。</p>
            <p v-else class="small muted">技术运行记录尚未取得。</p>
            <div class="hstack wrap">
              <button type="button" class="btn secondary" @click="emit('navigate', 'activity')">查看技术记录</button>
              <button type="button" class="btn secondary" @click="emit('navigate', 'report')">查看详细报告</button>
              <button type="button" class="btn secondary" @click="emit('navigate', 'publish')">发布到 GitHub</button>
            </div>
          </div>
        </details>
      </div>
    </section>
  </div>
</template>
