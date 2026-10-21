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
const extraWarnings = computed(() => [...new Set(props.warnings)]
  .filter(warning => !facts.value.boundaries.some(row => row.description === warning)));
</script>

<template>
  <section class="ov-results" aria-labelledby="overview-results-title">
    <header class="ov-section-head"><div><h2 id="overview-results-title">关键结果</h2></div><button type="button" class="ov-link" @click="emit('navigate', 'scope')">覆盖详情 <AppIcon name="external" :size="14" /></button></header>
    <div class="ov-panel ov-metrics">
      <button type="button" class="ov-metric" @click="emit('navigate', 'findings')">
        <span class="ov-metric__title"><AppIcon name="shield" :size="16" />高优先级问题</span>
        <strong :class="{ 'ov-count-danger': facts.blockers !== null && facts.blockers > 0 }">{{ facts.blockers ?? "未知" }}</strong>
        <small>{{ facts.blockers === null ? "摘要未提供准确数量" : facts.blockers > 0 ? "按审查策略建议合并前处理" : "没有达到高优先级标准的证据" }}</small>
      </button>
      <button type="button" class="ov-metric" @click="emit('navigate', 'findings')">
        <span class="ov-metric__title"><AppIcon name="help" :size="16" />待确认疑点</span>
        <strong>{{ facts.unresolved ?? "未知" }}</strong>
        <small>尚未确认缺陷 · 不含关联线索</small>
      </button>
      <button type="button" class="ov-metric" @click="emit('navigate', 'scope')">
        <span class="ov-metric__title"><AppIcon name="units" :size="16" />约定目标已检查</span>
        <strong>{{ facts.examined ?? "未知" }}<small v-if="facts.total !== null"> / {{ facts.total }}</small></strong>
        <small>静态检查目标</small>
      </button>
    </div>
    <p v-if="stale" class="ov-note"><AppIcon name="info" :size="16" /><span>以上数量来自旧快照，不适用于最新 Head。</span></p>
  </section>
  <section class="ov-panel ov-boundary" aria-labelledby="overview-boundary-title">
    <header class="ov-section-head"><div><h2 id="overview-boundary-title">结论依据与可信边界</h2></div></header>
    <div class="ov-boundary__rows">
      <button v-for="row in facts.boundaries" :key="row.title" type="button" class="ov-boundary__row" :aria-label="`查看${row.title}详情`" @click="emit('navigate', row.target)">
        <span class="ov-state-icon" :data-tone="row.tone"><AppIcon :name="row.icon" :size="18" /></span>
        <span class="ov-boundary__copy"><strong>{{ row.title }} <em class="ov-tag" :data-tone="row.tone">{{ row.value }}</em></strong><small v-if="row.description">{{ row.description }}</small></span>
        <AppIcon name="arrow-right" :size="16" />
      </button>
    </div>
    <ul v-if="extraWarnings.length" class="ov-limitations"><li v-for="warning in extraWarnings" :key="warning">{{ warning }}</li></ul>
  </section>
</template>
