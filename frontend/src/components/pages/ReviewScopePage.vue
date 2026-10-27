<script setup lang="ts">
import { computed, ref } from 'vue';
import type { ReviewTask } from '../../types/review';
import type { PublicationPlan } from '../../types/publication';
import EmptyState from '../common/EmptyState.vue';
const props = defineProps<{ task: ReviewTask; plan: PublicationPlan | null }>();
const filter = ref('all');
const goals = computed(() => (props.task.review_report?.goals || []).filter(goal => filter.value === 'all' || goal.status !== 'examined'));
</script>
<template>
  <div class="page-head"><div><h2>覆盖与限制</h2><p class="title-sub">同一源码范围只计一次，完成读取不自动证明缺陷已核验。</p></div><select v-model="filter" class="select rg-auto-width" aria-label="覆盖筛选"><option value="all">全部源码范围</option><option value="incomplete">仅未完成范围</option></select></div>
  <section class="card card-pad"><EmptyState v-if="!task.review_report" icon="units" title="覆盖范围尚未结算" description="运行中尚未形成最终记录，不能显示为零缺口或完整覆盖。" /><template v-else><div class="rg-table-scroll"><table class="rg-table"><thead><tr><th>文件 / 侧别</th><th>源码区间</th><th>检查状态</th><th>限制</th></tr></thead><tbody><tr v-for="goal in goals" :key="goal.id"><td><code>{{ goal.path }}</code><small>{{ goal.side === 'base' ? 'BASE' : 'HEAD' }}</small></td><td>{{ goal.start_line }}–{{ goal.end_line }}</td><td><span class="badge" :class="goal.status === 'examined' ? 'blue' : 'amber'">{{ {examined:'已检查',blocked:'受阻',not_examined:'未检查'}[goal.status] }}</span></td><td>{{ goal.reason || '—' }}</td></tr></tbody></table></div><p v-if="!goals.length" class="muted small">当前筛选下没有源码范围。</p></template></section>
  <section v-if="plan?.digest.excluded_files.length" class="card card-pad space-top"><div class="card-header"><h3>未纳入主审的文件</h3></div><p v-for="file in plan.digest.excluded_files" :key="file.path"><code>{{ file.path }}</code> · {{ file.reason }}</p></section>
</template>
