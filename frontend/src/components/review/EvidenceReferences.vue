<script setup lang="ts">
import { computed } from "vue";
import type { CrossUnitRelationship, UnitEvidenceReference } from "../../types/review";
import { evidenceSnapshotState } from "../../utils/reviewObservability";

const props = defineProps<{ ids: string[]; catalog: UnitEvidenceReference[]; headSha?: string; baseSha?: string; relationships?: CrossUnitRelationship[] }>();
const references = computed(() => [...new Set(props.ids)].map((id) => ({ id, reference: props.catalog.find((item) => item.id === id), relationship: props.relationships?.find((item) => item.id === id) })));
</script>

<template>
  <details v-if="references.length" class="evidence-references">
    <summary>证据引用 · {{ references.length }}</summary>
    <ul>
      <li v-for="item in references" :key="item.id">
        <template v-if="item.reference">
          <code>{{ item.reference.file_path }}<template v-if="item.reference.start_line > 0">:{{ item.reference.start_line }}<template v-if="item.reference.end_line !== item.reference.start_line">–{{ item.reference.end_line }}</template></template></code>
          <span>{{ item.reference.source === 'diff' ? '变更片段' : item.reference.source === 'context' ? '上下文' : '来源未记录' }} · {{ item.reference.start_line > 0 ? '已记录行范围' : '行号未记录' }}</span>
          <strong v-if="evidenceSnapshotState(item.reference, baseSha, headSha) === 'stale'" class="text-danger">引用属于其他 Base / Head 快照</strong>
          <strong v-else-if="evidenceSnapshotState(item.reference, baseSha, headSha) === 'unknown'">快照绑定未完整保存</strong>
          <small>Head {{ item.reference.head_sha?.slice(0, 12) || '未记录' }} · Base {{ item.reference.base_sha?.slice(0, 12) || '未记录' }}</small>
          <small><code>{{ item.id }}</code> · 内容哈希 <code>{{ item.reference.content_hash?.slice(0, 16) || '未记录' }}</code></small>
        </template>
        <template v-else-if="item.relationship"><code>{{ item.relationship.source_file }} → {{ item.relationship.target_file }}</code><span>静态关联引用 · {{ item.relationship.parser_id || '来源未记录' }}</span><small>{{ item.relationship.provenance }}</small><small>来自保存的索引关系，不是代码锚点或缺陷证明。</small></template>
        <template v-else><code>{{ item.id }}</code><span>引用元数据未保存，无法从当前记录追溯。</span></template>
      </li>
    </ul>
    <p>这里展示引用位置与快照绑定；引用元数据不包含完整代码正文。</p>
  </details>
</template>

<style scoped>
.evidence-references { margin-top: 8px; color: var(--text-secondary); font-size: 13px; }
summary { cursor: pointer; color: var(--primary); padding: 3px 0; }
ul { list-style: none; padding: 0; margin: 8px 0; }
li { display: grid; gap: 4px; padding: 9px 12px; border-left: 2px solid var(--border-strong); background: var(--surface-subtle); margin-bottom: 5px; overflow-wrap: anywhere; }
small { font-size: 11px; color: var(--text-secondary); }
p { font-size: 12px; margin: 6px 0; }
</style>
