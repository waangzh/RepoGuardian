<script setup lang="ts">
import type { ChangedFile } from "../types/review";

defineProps<{
  files: ChangedFile[];
}>();
</script>

<template>
  <section class="panel">
    <div class="panel-head">
      <h2>变更文件</h2>
      <span>{{ files.length }}</span>
    </div>
    <div v-if="files.length === 0" class="empty">暂无 diff 数据</div>
    <div v-else class="file-list">
      <div v-for="file in files" :key="file.file_path" class="file-row">
        <div>
          <strong>{{ file.file_path }}</strong>
          <small>{{ file.change_type }}</small>
        </div>
        <div class="delta">
          <span class="add">+{{ file.additions }}</span>
          <span class="del">-{{ file.deletions }}</span>
        </div>
      </div>
    </div>
  </section>
</template>

