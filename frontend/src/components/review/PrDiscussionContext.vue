<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, ref, watch } from "vue";
import type { DiscussionStats } from "../../types/prDiscussion";
import { discussionWorkspaceKey, usePrDiscussions } from "../../composables/usePrDiscussions";
import { discussionNotice } from "../../utils/prDiscussionPresentation";
import AppIcon from "../common/AppIcon.vue";
import DiscussionWorkbench from "./discussion/DiscussionWorkbench.vue";
import "../../styles/pr-discussions.css";
const props = defineProps<{ reviewId: string; stats?: DiscussionStats | null }>();
const workspace = inject(discussionWorkspaceKey, null);
const store = workspace?.store || usePrDiscussions(() => props.reviewId);
const opened = ref(false), trigger = ref<HTMLButtonElement>();
const stats = computed(() => store.stats.value || props.stats);
const recorded = computed(() => stats.value && stats.value.status !== "not_recorded");
function open() { if (workspace) workspace.open(); else opened.value = true; }
async function back() { opened.value = false; await nextTick(); trigger.value?.focus(); }
watch(() => props.reviewId, () => { opened.value = false; });
onBeforeUnmount(() => { if (!workspace) store.cancel(); });
</script>
<template>
  <DiscussionWorkbench v-if="opened" :store="store" :initial-stats="stats" @back="back" />
  <section v-else class="pd pd-summary pd-panel" aria-label="已有 PR 讨论">
    <header class="pd-module-head"><div class="pd-row"><span class="pd-boxicon"><AppIcon name="context" :size="22" /></span><div><h2>已有 PR 讨论</h2><p>历史评论及其在当前代码快照中的复核情况</p></div></div><span v-if="recorded" class="pd-pill gray">固定讨论快照</span></header>
    <template v-if="recorded && stats?.status !== 'unavailable' && !(stats?.status === 'complete' && stats.collected === 0)">
      <div class="pd-summary-grid"><div class="pd-module-stat"><b>{{ stats?.collected }}</b><span>已采集 {{ stats?.collected }} 项讨论 · {{ stats?.comments }} 条评论</span></div><div class="pd-module-stat"><b>{{ stats?.checked }}</b><span>有代码核验结论</span></div><div class="pd-module-stat"><b>{{ stats?.unchecked }}</b><span>尚未核验充分 · 未核验 {{ stats?.unchecked }} 项</span></div></div>
      <p class="pd-reference">已参考 {{ stats?.referenced }} 项相关讨论，包含 {{ stats?.referenced_comments }} 条评论。</p>
    </template>
    <p v-if="!recorded || stats?.status !== 'complete'" class="pd-notice amber"><AppIcon name="alert" :size="17" />{{ discussionNotice(stats) }}</p>
    <p v-else-if="stats.collected === 0" class="pd-reference">{{ discussionNotice(stats) }}</p>
    <details v-if="stats?.limitations.length" class="pd-limits"><summary>采集限制（{{ stats.limitations.length }}）</summary><ul><li v-for="(value, index) in stats.limitations" :key="index">{{ value }}</li></ul></details>
    <details v-if="stats?.collected" class="pd-limits"><summary>讨论统计口径</summary><p>线程已解决不代表代码问题已修复；历史讨论不计入本轮新增问题数量。</p></details>
    <footer class="pd-module-foot"><button ref="trigger" type="button" class="pd-btn primary" @click="open">查看讨论与核验详情 <AppIcon name="arrow-right" :size="15" /></button></footer>
  </section>
</template>
