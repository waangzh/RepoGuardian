<script setup lang="ts">
import { computed, ref } from 'vue'; import type { Finding, Run } from '../../types/review'; import AppIcon from '../common/AppIcon.vue'; import EmptyState from '../common/EmptyState.vue'; import ReviewDiff from '../review/diff/ReviewDiff.vue';
const props = defineProps<{ task: Run; findings: Finding[] }>(); defineEmits<{ finding: [id: string] }>();
const selected = ref(0), query = ref('');
const files = computed(() => props.task.changed_files.map((file,index) => ({file,index})).filter(({file}) => file.file_path.toLowerCase().includes(query.value.toLowerCase())));
const file = computed(() => props.task.changed_files[selected.value]);
const related = computed(() => props.findings.filter(item => item.location?.path === file.value?.file_path));
</script>
<template>
<div class="page-head">
<div>
<h2>变更</h2>
<p class="title-sub">按固定 Base / Head 查看 Diff、关联 Finding 与双侧源码。</p>
</div>
<span class="badge neutral">{{ task.changed_files.length }} 个文件</span>
</div>
<EmptyState v-if="!task.changed_files.length" icon="file-code" title="没有变更文件" description="没有文件记录不能被解释为审查已完成。" />
<div v-else class="rg-file-workbench">
<aside class="card rg-file-nav">
<input v-model="query" class="input" placeholder="搜索文件路径" />
<button v-for="entry in files" :key="entry.index" :class="{ active: selected === entry.index }" @click="selected = entry.index">
<AppIcon name="file-code" />
<span>
<strong>{{ entry.file.file_path.split('/').pop() }}</strong>
<small>{{ entry.file.file_path }}</small>
</span>
<small>+{{ entry.file.additions }} / −{{ entry.file.deletions }}</small>
</button>
</aside>
<div v-if="file" class="section-stack">
<div v-if="related.length" class="notice">
<strong>关联问题</strong>
<p v-for="finding in related" :key="finding.id">{{ finding.claim }}</p>
</div>
<ReviewDiff :key="task.id + '-' + selected" :file="file" :file-id="selected" :task-id="task.id" :snapshot="task.snapshot" />
</div>
</div>
</template>
