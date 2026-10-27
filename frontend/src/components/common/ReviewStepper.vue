<script setup lang="ts">
import AppIcon from "./AppIcon.vue";
defineProps<{ current: number }>();
</script>
<template>
  <nav class="stepper" aria-label="审查流程">
    <template v-for="(label, index) in ['导入 PR', '确认方案', '执行审查', '查看结果']" :key="label">
      <div class="stepper-item" :class="{ current: index === current, done: index < current }" :aria-current="index === current ? 'step' : undefined"><span class="stepper-num"><AppIcon v-if="index < current" name="check" :size="14" :stroke-width="2.4" /><template v-else>{{ index + 1 }}</template></span><span class="stepper-label">{{ label }}</span></div><div v-if="index < 3" class="stepper-line" />
    </template>
  </nav>
</template>
