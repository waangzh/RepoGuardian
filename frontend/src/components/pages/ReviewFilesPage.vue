<script setup lang="ts">
import { computed, ref } from "vue";
import type { ReviewTask } from "../../types/review";
import type { RuntimeFindingRow } from "../../utils/taskRuntimePresentation";
import { evidenceLabel, fileMatches } from "../../utils/workspacePresentation";
import ReviewDiff from "../review/ReviewDiff.vue";
import EmptyState from "../common/EmptyState.vue";
import AppIcon from "../common/AppIcon.vue";
import ReviewFileTree from "../review/ReviewFileTree.vue";
import { buildFileTree } from "../../utils/fileTree";
const props = defineProps<{ task: ReviewTask; findings: RuntimeFindingRow[]; initialPath?: string }>();
const emit = defineEmits<{ finding: [key: string] }>();
const path = ref(props.initialPath || '');
const search = ref("");
const visible = computed(() => props.task.changed_files.filter(file => `${file.file_path} ${file.old_file_path || ''}`.toLowerCase().includes(search.value.toLowerCase())));
const selected = computed(() => visible.value.find(file => file.file_path === path.value) || visible.value[0] || null);
const related = computed(() => selected.value ? props.findings.filter(row => fileMatches(selected.value!, row.filePath)) : []);
const selectedIndex = computed(() => selected.value ? props.task.changed_files.indexOf(selected.value) : -1);
const fileTree = computed(() => buildFileTree(visible.value.map(file => file.file_path)));
</script>
<template>
  <div class="page-head"><div><h1>变更文件</h1><p class="title-sub">按文件查看真实代码差异与关联发现。文件列表不推断代码正确性。</p></div><span class="badge blue">{{ task.changed_files.length }} 个变更文件</span></div>
  <p v-if="initialPath && !task.changed_files.some(file => file.file_path === initialPath)" class="notice warn" role="status">{{ initialPath }} 不在本轮变更文件列表中；请通过报告中的快照源码链接核对关联文件。</p>
  <div class="rg-files-layout">
    <aside class="card card-pad rg-file-nav">
      <h2>文件列表</h2>
      <div class="rg-file-search">
        <AppIcon name="search" :size="16" />
        <input id="file-search" v-model="search" class="input" type="search" aria-label="搜索变更文件" placeholder="筛选文件…" />
      </div>
      <nav aria-label="变更文件目录">
        <ReviewFileTree :nodes="fileTree" :selected="selected?.file_path || ''" :filter-key="search" @select="path = $event" />
      </nav>
      <p v-if="!visible.length" class="muted small space-top">没有匹配文件。</p>
    </aside>
    <div class="section-stack">
      <ReviewDiff :key="`${task.id}:${selectedIndex}`" :file="selected" :task-id="task.id" :file-id="selectedIndex" :snapshot="task.snapshot" />
      <section class="card card-pad"><div class="card-header"><div><h2>与此文件关联的发现</h2><p>仅按真实文件路径关联</p></div></div><button v-for="row in related" :key="row.key" type="button" class="recent-row" @click="emit('finding', row.key)"><span class="recent-identity"><strong>{{ row.title }}</strong><small>{{ evidenceLabel(row.status, row.issue) }}</small></span><AppIcon name="arrow-right" :size="16" /></button><EmptyState v-if="!related.length" icon="context" title="当前文件未形成可展示的发现" description="仍需结合检查范围与局限判断。" /></section>
    </div>
  </div>
</template>
