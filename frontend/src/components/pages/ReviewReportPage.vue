<script setup lang="ts">
import { ref } from 'vue';
import type { ReviewTask } from '../../types/review';
import type { PublicationPlan } from '../../types/publication';
import AppIcon from '../common/AppIcon.vue';
import ReportMarkdown from '../report/ReportMarkdown.vue';
import GitHubPublicationPanel from '../review/publication/GitHubPublicationPanel.vue';
defineProps<{ task: ReviewTask; plan: PublicationPlan | null; report: string; loading: boolean }>();
defineEmits<{ export: [] }>();
const mode = ref<'readable' | 'comment' | 'audit'>('readable');
</script>
<template>
  <div class="rd-workspace"><div class="rd-heading"><div><h2>报告与交付</h2><p>从阅读结论到核对证据，再到复制评论或显式发布。</p></div><button class="btn secondary" :disabled="!plan" @click="$emit('export')"><AppIcon name="download" />导出此快照报告</button></div>
    <div class="rd-shell"><div class="rd-shell-top"><nav class="rd-tabs" aria-label="报告模式"><button v-for="item in ([['readable','审查报告'],['comment','PR 评论与发布'],['audit','技术记录']] as const)" :key="item[0]" :class="{ active: mode === item[0] }" :aria-current="mode === item[0] ? 'page' : undefined" @click="mode = item[0]">{{ item[1] }}</button></nav><small class="rd-shell-caption">固定快照记录</small></div>
      <div class="rd-layout"><main class="rd-main"><p v-if="loading" role="status">正在读取报告…</p><p v-else-if="!plan" class="muted">审查尚未形成可读取的交付计划。</p>
        <ReportMarkdown v-if="mode === 'readable' && plan" :content="plan.user_report_markdown" />
        <GitHubPublicationPanel v-else-if="mode === 'comment' && plan" :review-id="task.id" :plan="plan" />
        <template v-else-if="mode === 'audit'"><p class="notice small">结构化图执行记录来自本任务，包含候选、核验及覆盖；不包含共享消息历史。</p><pre v-if="task.review_report" class="rg-json space-top">{{ JSON.stringify(task.review_report,null,2) }}</pre><p v-else class="muted space-top">尚无已保存的图执行记录。</p><details v-if="report" class="space-top"><summary class="link">完整归档报告</summary><ReportMarkdown :content="report" /></details></template>
      </main><aside class="rd-aside"><section class="rd-aside-card"><h3>本次快照</h3><p>HEAD</p><code class="break-anywhere">{{ task.snapshot.head_sha }}</code><p class="space-top">有效 BASE</p><code class="break-anywhere">{{ task.snapshot.effective_base_sha }}</code></section><section class="rd-aside-card"><h3>交付边界</h3><p>导出保留原快照，不表示已核对最新 PR。复制会再次检查 Head；直接发布需要预检及二次确认。</p><p>未执行目标仓库代码、测试或构建。</p></section></aside></div>
    </div>
  </div>
</template>
