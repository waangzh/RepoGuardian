<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { ReviewTask, TaskRuntimeReport } from "../../types/review";
import { runtimeLabel, taskRuntimeSummary, retryableRuntimeUnits } from "../../utils/taskRuntimePresentation";
import TaskRuntimePanel from "../review/runtime/TaskRuntimePanel.vue";
import EmptyState from "../common/EmptyState.vue";
const props = defineProps<{ task: ReviewTask; runtime: TaskRuntimeReport | null; terminal: boolean; retryingUnitId: string | null; initialFilter?: "all" | "incomplete"; investigationsOpen?: boolean }>();
const emit = defineEmits<{ retry: [unitId: string] }>();
const filter = ref("all");
watch(() => props.initialFilter, value => { filter.value = value || "all"; }, { immediate: true });
const summary = computed(() => props.runtime ? taskRuntimeSummary(props.runtime) : null);
const visible = computed(() => (props.runtime?.initial_scope_coverage || []).filter(item => filter.value === 'all' || (filter.value === 'examined' ? item.status === 'examined' : item.status !== 'examined')));
const retryUnits = computed(() => props.runtime ? retryableRuntimeUnits(props.runtime) : []);
</script>
<template>
  <div class="page-head"><div><h1>覆盖范围与审查局限</h1><p class="title-sub">约定目标已检查、证据充分与代码正确是不同结论。</p></div><span class="badge blue">{{ summary ? `${summary.scope.examined} / ${summary.scope.total} 目标已检查` : '目标覆盖未知' }}</span></div>
  <div class="section-stack"><div class="kpis"><section v-for="item in [['目标已检查', summary?.scope.examined], ['未完成目标', summary?.scope.incomplete], ['补充调查', summary?.investigations.total], ['调查未执行', summary?.investigations.not_executed]]" :key="item[0]" class="card kpi"><div class="cap">{{ item[0] }}</div><div class="number">{{ item[1] ?? '未知' }}</div></section></div>
    <section class="card card-pad"><div class="card-header"><div><h2>约定的审查目标</h2><p>分母为任务快照中的初始目标，不是业务路径或测试覆盖率</p></div></div><div class="segmented"><button v-for="item in ([['all', '全部'], ['examined', '已检查'], ['incomplete', '未完成 / 未知']] as const)" :key="item[0]" type="button" :class="{ active: filter === item[0] }" :aria-pressed="filter === item[0]" @click="filter = item[0]">{{ item[1] }}</button></div><div class="rg-table-wrap"><table class="rg-table"><thead><tr><th>文件与范围</th><th>检查状态</th><th>依据与限制</th></tr></thead><tbody><tr v-for="(goal, index) in visible" :key="String(goal.goal_id || index)"><td><code>{{ goal.target_ref || '未记录目标' }}<template v-if="goal.target_kind === 'source_range'">:{{ goal.start_line }}–{{ goal.end_line }}</template></code></td><td><span class="badge" :class="goal.status === 'examined' ? 'blue' : 'warn'">{{ runtimeLabel(goal.status) }}</span></td><td>{{ goal.reason || '未提供检查说明' }}</td></tr></tbody></table></div><EmptyState v-if="!visible.length" icon="shield" title="当前分类没有目标记录" description="缺失的目标记录保持未知，不推断已经检查。" /><div v-if="terminal && retryUnits.length" class="hstack wrap space-top"><button v-for="unit in retryUnits" :key="unit" type="button" class="btn secondary" :disabled="!!retryingUnitId" @click="emit('retry', unit)">{{ retryingUnitId === unit ? '重新排队中…' : '重新检查缺口' }} · {{ unit }}</button></div></section>
    <details v-if="runtime" :open="investigationsOpen" class="card card-pad"><summary class="rg-disclosure-title">补充调查、关联线索与完整检查依据</summary><p class="muted small space-top">关联线索不计入已确认问题或已完成调查；展开查看未执行和证据不足的原因。</p><TaskRuntimePanel :report="runtime" :terminal="terminal" :retrying-unit-id="retryingUnitId" view="checks" @retry="emit('retry', $event)" /></details>
    <section v-if="task.excluded_files.length" class="card card-pad"><h2>策略排除的文件</h2><p v-for="file in task.excluded_files" :key="file.file_path" class="small space-top"><code class="break-anywhere">{{ file.file_path }}</code> · {{ file.reason }}</p></section>
  </div>
</template>
