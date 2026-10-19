<script setup lang="ts">
import { computed } from "vue";
import type { ReviewSummary } from "../../types/publication";
import type { ReviewSection } from "../../types/workspace";
import type { TaskRuntimeSummary } from "../../utils/taskRuntimePresentation";
import { overviewFacts } from "../../utils/overviewPresentation";
import AppIcon from "../common/AppIcon.vue";

const props = defineProps<{ plan: ReviewSummary | null; runtime: TaskRuntimeSummary | null; warnings: string[]; stale?: boolean }>();
const emit = defineEmits<{ navigate: [section: ReviewSection] }>();
const facts = computed(() => overviewFacts(props.plan, props.runtime));
</script>

<template>
  <section class="ov-results" aria-labelledby="overview-results-title">
    <header class="ov-section-head"><div><h2 id="overview-results-title">关键结果</h2><p>问题发现、目标覆盖与验证能力分别统计，不合并成“审查通过率”。</p></div><button type="button" class="ov-link" @click="emit('navigate', 'scope')">覆盖详情 <AppIcon name="external" :size="14" /></button></header>
    <div class="ov-panel ov-metrics">
      <button type="button" class="ov-metric" @click="emit('navigate', 'findings')">
        <span class="ov-metric__title"><AppIcon name="shield" :size="16" />阻断问题</span>
        <strong :class="{ 'ov-count-danger': facts.blockers !== null && facts.blockers > 0 }">{{ facts.blockers ?? "未知" }}</strong>
        <small>{{ facts.blockers === null ? "摘要未提供准确数量" : facts.blockers > 0 ? "需要优先核对和修复" : "没有达到阻断标准的证据" }}</small>
      </button>
      <button type="button" class="ov-metric" @click="emit('navigate', 'findings')">
        <span class="ov-metric__title"><AppIcon name="help" :size="16" />待确认候选</span>
        <strong>{{ facts.unresolved ?? "未知" }}</strong>
        <small>不包含未核验的关联线索</small>
      </button>
      <button type="button" class="ov-metric" @click="emit('navigate', 'scope')">
        <span class="ov-metric__title"><AppIcon name="units" :size="16" />约定目标已检查</span>
        <strong>{{ facts.examined ?? "未知" }}<small v-if="facts.total !== null"> / {{ facts.total }}</small></strong>
        <small>不等于所有路径已验证</small>
      </button>
    </div>
    <p class="ov-note"><AppIcon name="info" :size="16" /><span>{{ stale ? "以上数量来自旧快照，不适用于最新 Head。" : "“已检查”指完成约定的静态检查目标；未核验关联线索、动态测试与业务规则不自动计入上述三个数字。" }}</span></p>
  </section>
  <section class="ov-panel ov-boundary" aria-labelledby="overview-boundary-title">
    <header class="ov-section-head"><div><h2 id="overview-boundary-title">结论依据与可信边界</h2><p>影响判断的限制直接呈现，详细记录可按需查看。</p></div></header>
    <div class="ov-boundary__rows">
      <button v-for="row in facts.boundaries" :key="row.title" type="button" class="ov-boundary__row" :aria-label="`查看${row.title}详情`" @click="emit('navigate', row.target)">
        <span class="ov-state-icon" :data-tone="row.tone"><AppIcon :name="row.icon" :size="18" /></span>
        <span class="ov-boundary__copy"><strong>{{ row.title }} <em class="ov-tag" :data-tone="row.tone">{{ row.value }}</em></strong><small>{{ row.description }}</small></span>
        <AppIcon name="arrow-right" :size="16" />
      </button>
    </div>
    <ul v-if="warnings.length" class="ov-limitations"><li v-for="warning in warnings" :key="warning">{{ warning }}</li></ul>
  </section>
</template>
