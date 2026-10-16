<script setup lang="ts">
defineProps<{ current: number }>();
</script>
<template>
  <nav class="stepper" aria-label="审查流程">
    <template v-for="(label, index) in ['导入 PR', '确认方案', '执行审查', '查看结果']" :key="label">
      <div class="stepper-item" :class="{ current: index === current, done: index < current }" :aria-current="index === current ? 'step' : undefined"><span class="stepper-num">{{ index < current ? '✓' : index + 1 }}</span>{{ label }}</div><div v-if="index < 3" class="stepper-line" />
    </template>
  </nav>
</template>
