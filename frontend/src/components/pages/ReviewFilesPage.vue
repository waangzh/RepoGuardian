<script setup lang="ts">
import { computed, ref } from "vue";
import type { ReviewTask } from "../../types/review";
import type { RuntimeFindingRow } from "../../utils/taskRuntimePresentation";
import { evidenceLabel, fileMatches } from "../../utils/workspacePresentation";
import ReviewDiff from "../review/ReviewDiff.vue";
import EmptyState from "../common/EmptyState.vue";
import AppIcon from "../common/AppIcon.vue";
const props = defineProps<{ task: ReviewTask; findings: RuntimeFindingRow[] }>();
const emit = defineEmits<{ finding: [key: string] }>();
const path = ref("");
const search = ref("");
const visible = computed(() => props.task.changed_files.filter(file => `${file.file_path} ${file.old_file_path || ''}`.toLowerCase().includes(search.value.toLowerCase())));
const selected = computed(() => visible.value.find(file => file.file_path === path.value) || visible.value[0] || null);
const related = computed(() => selected.value ? props.findings.filter(row => fileMatches(selected.value!, row.filePath)) : []);
</script>
<template>
  <div class="page-head"><div><h1>变更文件</h1><p class="title-sub">按文件查看真实代码差异与关联发现。文件列表不推断代码正确性。</p></div><span class="badge blue">{{ task.changed_files.length }} 个变更文件</span></div>
  <div class="rg-files-layout"><aside class="card card-pad rg-file-nav"><h2>文件列表</h2><label class="field-label space-top" for="file-search">搜索文件</label><input id="file-search" v-model="search" class="input" type="search" placeholder="文件名或路径" /><button v-for="file in visible" :key="file.file_path" type="button" :class="{ active: selected?.file_path === file.file_path }" :aria-pressed="selected?.file_path === file.file_path" @click="path = file.file_path"><AppIcon name="file" :size="16" /><span><strong class="mono break-anywhere">{{ file.file_path }}</strong><small>+{{ file.additions }} / −{{ file.deletions }} · {{ file.change_type }}</small><small v-if="file.old_file_path">原路径 {{ file.old_file_path }}</small></span></button><p v-if="!visible.length" class="muted small space-top">没有匹配文件。</p><div class="notice space-top"><AppIcon name="info" /><div>文件变更、目标覆盖与缺陷结论分别记录。未生成发现不表示文件没有问题。</div></div></aside><div class="section-stack"><ReviewDiff :file="selected" /><section class="card card-pad"><div class="card-header"><div><h2>与此文件关联的发现</h2><p>仅按真实文件路径关联</p></div></div><button v-for="row in related" :key="row.key" type="button" class="recent-row" @click="emit('finding', row.key)"><span class="recent-identity"><strong>{{ row.title }}</strong><small>{{ evidenceLabel(row.status, row.issue) }}</small></span><AppIcon name="arrow-right" :size="16" /></button><EmptyState v-if="!related.length" icon="context" title="当前文件未形成可展示的发现" description="仍需结合检查范围与局限判断。" /></section></div></div>
</template>
