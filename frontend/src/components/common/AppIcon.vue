<script setup lang="ts">
import { computed } from "vue";

const props = withDefaults(defineProps<{
  name: string;
  size?: number;
  strokeWidth?: number;
}>(), {
  size: 18,
  strokeWidth: 1.8,
});

const paths: Record<string, string[]> = {
  alert: ["M12 9v4", "M12 17h.01", "M10.3 3.6 2.4 18a2 2 0 0 0 1.8 3h15.6a2 2 0 0 0 1.8-3L13.7 3.6a2 2 0 0 0-3.4 0Z"],
  branch: ["M6 3v12", "M18 9V5", "M6 8h6a6 6 0 0 1 6 6v7", "M3 18a3 3 0 1 0 6 0 3 3 0 0 0-6 0Z", "M15 3a3 3 0 1 0 6 0 3 3 0 0 0-6 0Z"],
  check: ["m5 12 4 4L19 6"],
  "check-circle": ["M22 11.1V12a10 10 0 1 1-5.9-9.1", "m9 11 3 3L22 4"],
  clock: ["M12 8v5l3 2", "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20Z"],
  code: ["m8 9-3 3 3 3", "m16 9 3 3-3 3", "m14 5-4 14"],
  context: ["M8 3H5a2 2 0 0 0-2 2v3", "M16 3h3a2 2 0 0 1 2 2v3", "M8 21H5a2 2 0 0 1-2-2v-3", "M16 21h3a2 2 0 0 0 2-2v-3", "M8 12h8"],
  dashboard: ["M3 3h7v7H3z", "M14 3h7v4h-7z", "M14 11h7v10h-7z", "M3 14h7v7H3z"],
  file: ["M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z", "M14 2v6h6", "M8 13h8", "M8 17h8"],
  "file-code": ["M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z", "M14 2v6h6", "m10 13-2 2 2 2", "m14 13 2 2-2 2"],
  flask: ["M9 3h6", "M10 3v5L4.5 18a2 2 0 0 0 1.8 3h11.4a2 2 0 0 0 1.8-3L14 8V3", "M7.5 15h9"],
  help: ["M9.1 9a3 3 0 1 1 5.8 1c0 2-3 2-3 4", "M12 18h.01", "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20Z"],
  history: ["M3 12a9 9 0 1 0 3-6.7L3 8", "M3 3v5h5", "M12 7v5l3 2"],
  info: ["M12 16v-4", "M12 8h.01", "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20Z"],
  patch: ["M14.7 6.3a4 4 0 0 0-5-5L12 3.6 3.6 12a2 2 0 0 0 0 2.8l1.6 1.6a2 2 0 0 0 2.8 0L16.4 8l2.3 2.3a4 4 0 0 0-4-5Z"],
  play: ["M8 5v14l11-7Z"],
  report: ["M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z", "M14 2v6h6", "M8 13h8", "M8 17h5"],
  server: ["M4 3h16a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Z", "M4 13h16a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-4a2 2 0 0 1 2-2Z", "M6 7h.01", "M6 17h.01"],
  settings: ["M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z", "M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.1 3.6-.1-.1a1.7 1.7 0 0 0-1.8-.7 1.7 1.7 0 0 0-1.3 1.3V21H9.5v-.1a1.7 1.7 0 0 0-1.3-1.3 1.7 1.7 0 0 0-1.8.7l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.6-1H3V10h.1a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7l2.1-3.6.1.1a1.7 1.7 0 0 0 1.8.7A1.7 1.7 0 0 0 9.5 3V3h5v.1a1.7 1.7 0 0 0 1.3 1.3 1.7 1.7 0 0 0 1.8-.7l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.1v4H21a1.7 1.7 0 0 0-1.6 1Z"],
  shield: ["M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"],
  units: ["M4 4h6v6H4z", "M14 4h6v6h-6z", "M4 14h6v6H4z", "M14 14h6v6h-6z"],
  x: ["M18 6 6 18", "m6 6 12 12"],
};

const iconPaths = computed(() => paths[props.name] || paths.info);
</script>

<template>
  <svg
    class="app-icon"
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    :stroke-width="strokeWidth"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path v-for="path in iconPaths" :key="path" :d="path" />
  </svg>
</template>
