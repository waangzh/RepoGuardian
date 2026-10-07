<script setup lang="ts">
import { computed } from "vue";
import type { ReviewUnitResult } from "../../types/review";
import { checkLabels, unitRecordPresentation } from "../../utils/reviewObservability";
import StatusBadge from "../common/StatusBadge.vue";
import EmptyState from "../common/EmptyState.vue";
import EvidenceReferences from "./EvidenceReferences.vue";

const props = defineProps<{ result?: ReviewUnitResult; headSha?: string; baseSha?: string }>();
const summary = computed(() => props.result?.review_summary);
const presentation = computed(() => unitRecordPresentation(summary.value));
const record = computed(() => presentation.value.record);
const retained = computed(() => presentation.value.retained);
const evidence = computed(() => summary.value?.evidence || []);
const ledger = computed(() => props.result?.coverage_ledger);
const reviewedHunks = computed(() => Object.values(ledger.value?.diff_hunks || {}).filter((item) => item.status === "reviewed").length);
function hypothesis(id: string): string {
  return props.result?.plan?.risk_hypotheses.find((item) => item.id === id)?.description || `假设 ${id}`;
}
function tone(status: string): string {
  return status === "conflicting" ? "failed" : ["unresolved", "not_checked"].includes(status) ? "warning" : "neutral";
}
</script>

<template>
  <article class="unit-record">
    <header><div><h3>实际检查记录</h3><p>已检查表示执行过检查，不等于代码正确或问题已确认。</p></div><StatusBadge :status="presentation.tone" :label="presentation.label" /></header>
    <p v-if="retained" class="muted">以下为最近有效检查记录，不表示本轮已完成核验。{{ summary?.latest_attempt_reason }}</p>
    <section v-if="result?.diff_manifest && ledger"><h4>Hunk 覆盖 · {{ reviewedHunks }} / {{ result.diff_manifest.hunks.length }}</h4>
      <p class="muted">清单展示完整变更；模型每轮只检查当前活动范围。已检查不等于正确性证明。</p>
      <details class="record-history"><summary>查看完整变更清单与覆盖状态</summary>
        <p v-for="hunk in result.diff_manifest.hunks" :key="hunk.id">{{ hunk.file_path }} · {{ hunk.hunk_id }} · {{ ledger.diff_hunks[hunk.id]?.status || 'pending' }}</p>
      </details>
      <p class="muted">待解决问题 {{ Object.values(ledger.questions).filter((item) => item.status === 'pending').length }} 个</p>
    </section>
    <section v-if="result?.diff_batches?.length"><h4>分批审查 · {{ result.diff_batches.length }} 个工作集</h4>
      <p class="muted">每批仅检查其展示范围；未执行或失败的批次不计入完整覆盖。</p>
      <details v-for="(batch, index) in result.diff_batches" :key="batch.id" class="record-history">
        <summary>第 {{ index + 1 }} 批 · {{ batch.status === 'completed' ? '已检查' : '覆盖未完成' }}</summary>
        <p v-for="(range, rangeIndex) in batch.ranges" :key="rangeIndex">{{ range.file_path }} · Diff 行偏移 {{ range.start_offset }}–{{ range.end_offset }}</p>
        <p>{{ batch.reason }}</p>
        <p v-if="batch.result?.review_summary.record">{{ batch.result.review_summary.record.change_summary }}</p>
      </details>
    </section>
    <EmptyState v-if="!record" icon="help" title="尚无可用的结构化检查记录" description="可能尚未执行诊断，或旧任务、兼容模型未返回有效记录；不能据此判断无风险。" />
    <template v-else>
      <p class="record-summary">{{ record.change_summary }}</p>
      <section><h4>检查目标</h4>
        <p v-if="!record.target_checks.length" class="muted">未记录检查目标。</p>
        <article v-for="item in record.target_checks" :key="item.target" class="check-row">
          <div><strong>{{ item.target }}</strong><StatusBadge :status="tone(item.status)" :label="checkLabels[item.status]" /></div>
          <p>{{ item.reason }}</p><EvidenceReferences :ids="item.evidence_ids" :catalog="evidence" :head-sha="headSha" :base-sha="baseSha" />
        </article>
      </section>
      <section v-if="record.hypothesis_checks.length"><h4>风险假设核验</h4>
        <article v-for="item in record.hypothesis_checks" :key="item.hypothesis_id" class="check-row">
          <div><strong>{{ hypothesis(item.hypothesis_id) }}</strong><StatusBadge :status="tone(item.status)" :label="checkLabels[item.status]" /></div>
          <p>{{ item.reason }}</p><EvidenceReferences :ids="item.evidence_ids" :catalog="evidence" :head-sha="headSha" :base-sha="baseSha" />
        </article>
      </section>
      <section v-if="record.contract_dependencies.length"><h4>依赖与契约</h4>
        <article v-for="(item, index) in record.contract_dependencies" :key="`${item.file_path}:${index}`" class="check-row">
          <div><code>{{ item.file_path }}<template v-if="item.symbol"> · {{ item.symbol }}</template></code><StatusBadge :status="tone(item.status)" :label="checkLabels[item.status]" /></div>
          <p>{{ item.assumption }}</p><EvidenceReferences :ids="item.evidence_ids" :catalog="evidence" :head-sha="headSha" :base-sha="baseSha" />
        </article>
      </section>
      <section v-if="record.unresolved_questions.length" class="unresolved-questions"><h4>仍需确认的问题 · {{ record.unresolved_questions.length }}</h4>
        <article v-for="(item, index) in record.unresolved_questions" :key="index" class="check-row">
          <strong>{{ item.question }}</strong><p>{{ item.affected_files.join('、') || '影响范围尚未明确' }}</p>
          <EvidenceReferences :ids="item.evidence_ids" :catalog="evidence" :head-sha="headSha" :base-sha="baseSha" />
        </article>
      </section>
      <details v-if="summary?.record_history?.length" class="record-history"><summary>查看诊断轮次 · {{ summary.record_history.length }}</summary>
        <article v-for="(item, index) in summary.record_history" :key="index"><strong>第 {{ index + 1 }} 轮 · {{ item.change_summary }}</strong><p>{{ item.target_checks.length }} 个目标记录 · {{ item.unresolved_questions.length }} 个未决问题</p></article>
        <p>上方展示累计记录；不同轮次的检查范围可能不同。</p>
      </details>
    </template>
    <details v-if="summary?.reason || result?.terminal_reason" class="record-history"><summary>记录与执行元数据</summary><p>记录状态：{{ summary?.status || '未保存' }}</p><code>{{ summary?.reason }}</code><p>执行结束原因：{{ result?.terminal_reason || '尚未结束或未记录' }}</p></details>
  </article>
</template>

<style scoped>
.unit-record { min-width: 0; padding: 22px; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-md); }
header, .check-row > div { display: flex; justify-content: space-between; align-items: flex-start; gap: 14px; }
header h3 { margin: 0 0 6px; font-size: 17px; }
header p, .muted { color: var(--text-secondary); font-size: 13px; line-height: 1.6; margin: 0; }
.record-summary { padding: 14px 0; margin: 8px 0 0; line-height: 1.7; overflow-wrap: anywhere; }
section { margin-top: 20px; }
h4 { font-size: 13px; letter-spacing: .04em; margin: 0 0 10px; color: var(--text-secondary); }
.check-row { padding: 12px 0; border-top: 1px solid var(--border); overflow-wrap: anywhere; }
.check-row strong, .check-row code { font-size: 14px; line-height: 1.65; }
.check-row p { color: var(--text-secondary); font-size: 13px; margin: 6px 0 0; line-height: 1.65; }
.unresolved-questions { border-left: 3px solid var(--warning); padding-left: 14px; }
.record-history { font-size: 12px; color: var(--text-secondary); margin-top: 18px; padding-top: 12px; border-top: 1px solid var(--border); overflow-wrap: anywhere; }
.record-history summary { cursor: pointer; font-weight: 600; }
.record-history article { padding-top: 12px; }
.record-history p { margin: 8px 0; }
@media (max-width: 600px) { .unit-record { padding: 16px; } header, .check-row > div { flex-direction: column; gap: 8px; } }
</style>
