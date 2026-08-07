<script setup lang="ts">
import { computed } from "vue";
import type { ChangedFile } from "../types/review";
import EmptyState from "./common/EmptyState.vue";
import PanelHeader from "./common/PanelHeader.vue";

const props = defineProps<{
  files: ChangedFile[];
}>();

const additions = computed(() => props.files.reduce((sum, file) => sum + file.additions, 0));
const deletions = computed(() => props.files.reduce((sum, file) => sum + file.deletions, 0));

function extension(path: string): string {
  const value = path.split(".").pop();
  return value && value !== path ? value.toUpperCase() : "FILE";
}
</script>

<template>
  <section class="panel changed-files-panel">
    <PanelHeader icon="file-code" title="变更文件" subtitle="Changed Files">
      <div class="file-totals"><span>{{ files.length }} 个文件</span><b class="text-success">+{{ additions }}</b><b class="text-danger">-{{ deletions }}</b></div>
    </PanelHeader>
    <EmptyState v-if="files.length === 0" icon="file-code" title="尚未加载变更文件" description="任务准备完成后，系统会在这里列出本次 PR 的变更范围。" />
    <div v-else class="file-list">
      <div v-for="file in files" :key="file.file_path" class="file-row">
        <div class="file-row__main">
          <code>{{ file.file_path }}</code>
          <div><span class="file-type">{{ extension(file.file_path) }}</span><small>{{ file.change_type }}<template v-if="file.is_binary"> · binary</template></small></div>
        </div>
        <div class="delta">
          <span class="add">+{{ file.additions }}</span>
          <span class="del">-{{ file.deletions }}</span>
        </div>
      </div>
    </div>
  </section>
</template>
