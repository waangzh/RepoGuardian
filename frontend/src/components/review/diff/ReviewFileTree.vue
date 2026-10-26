<script setup lang="ts">
import type { FileTreeNode } from "../../../utils/fileTree";
import AppIcon from "../../common/AppIcon.vue";

withDefaults(defineProps<{ nodes: FileTreeNode[]; selected: string; filterKey?: string; depth?: number }>(), {
  filterKey: "", depth: 0,
});
const emit = defineEmits<{ select: [path: string] }>();
</script>

<template>
  <ul class="rg-file-tree" :style="{ '--tree-indent': `${Math.min(depth, 6) * 12}px` }">
    <li v-for="node in nodes" :key="`${node.kind}:${node.path}`">
      <details v-if="node.kind === 'directory'" :key="`${node.path}:${filterKey}`" class="rg-file-folder" open>
        <summary class="rg-file-tree-row" :title="node.path">
          <AppIcon name="chevron-down" :size="14" class="rg-file-tree-chevron" />
          <AppIcon name="folder" :size="16" class="rg-file-tree-folder" />
          <span>{{ node.label }}</span>
        </summary>
        <ReviewFileTree :nodes="node.children" :selected="selected" :filter-key="filterKey" :depth="depth + 1" @select="emit('select', $event)" />
      </details>
      <button v-else type="button" class="rg-file-tree-row rg-file-tree-file" :class="{ active: selected === node.path }"
        :aria-label="node.path" :aria-current="selected === node.path ? 'true' : undefined" :title="node.path"
        @click="emit('select', node.path)">
        <span aria-hidden="true" /><AppIcon name="file-code" :size="16" /><span>{{ node.label }}</span>
      </button>
    </li>
  </ul>
</template>
