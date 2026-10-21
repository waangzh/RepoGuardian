<script setup lang="ts">
import { computed } from 'vue';
import type { ReportMode } from './ReportModeTabs.vue';
import type { ReviewTask } from '../../types/review';
import type { PublicationPlan } from '../../types/publication';
import { reportChapters } from '../../utils/reportPresentation';
import AppIcon from '../common/AppIcon.vue';
const props = defineProps<{ mode: ReportMode; detail: 'compact' | 'full'; excludeResolved: boolean; available: boolean;
  busy: boolean; copied: boolean; headLabel: string; headTone: string; headNote: string; canCheck: boolean; canPublish: boolean;
  task?: ReviewTask; plan?: PublicationPlan | null; retryCheck?: boolean; stale?: boolean }>();
const emit = defineEmits<{ 'update:detail': [value: 'compact' | 'full']; 'update:excludeResolved': [value: boolean];
  copy: []; download: []; check: []; publish: []; full: [] }>();
const chapters = computed(() => props.plan ? reportChapters(props.plan) : [{ id: 'conclusion', label: '审查结论' }]);
</script>
<template>
  <aside class="rd-aside" aria-label="报告工具区">
    <section v-if="mode === 'audit'" class="rd-aside-card"><h3>运行元数据<AppIcon name="code" :size="16" /></h3><dl class="rd-aside-metadata"><dt>PR</dt><dd>{{ task?.pr ? `#${task.pr.number}` : '未记录' }}</dd><dt>Head</dt><dd><code>{{ plan?.snapshot.head_sha?.slice(0, 12) || '未记录' }}</code></dd><dt>运行模式</dt><dd>{{ task?.mode === 'review' ? '只读静态' : task?.mode || '未知' }}</dd><dt>原始事件</dt><dd>{{ task?.agent_events ? `当前返回 ${task.agent_events.length} 条` : '未提供' }}</dd></dl></section>
    <section v-if="mode === 'comment'" class="rd-aside-card">
      <h3>交付设置<AppIcon name="settings" :size="16" /></h3><p>控制复制到 PR 的信息密度，而不是改变机器审查结论。</p>
      <label class="rd-field">评论内容密度<select :value="detail" :disabled="!canCheck" aria-label="评论内容密度" @change="emit('update:detail', ($event.target as HTMLSelectElement).value as 'compact' | 'full')"><option value="compact">简洁评论 · 包含所有可交付问题及处理建议</option><option value="full">详细评论 · 增加证据、触发场景与审查范围</option></select></label>
      <label class="rd-checkrow"><input :checked="excludeResolved" :disabled="!canCheck" type="checkbox" @change="emit('update:excludeResolved', ($event.target as HTMLInputElement).checked)" /><span>排除已处理的非阻断事项<small>不会隐藏阻断问题</small></span></label>
      <div class="rd-safety"><AppIcon name="shield" :size="17" /><div><strong>不会改变审查事实</strong><small>评论选项仅调整交付内容</small></div></div>
    </section>
    <section v-if="mode === 'user'" class="rd-aside-card"><h3>章节导航</h3><nav class="rd-toc" aria-label="详细报告章节"><a v-for="chapter in chapters" :key="chapter.id" :href="`#rd-detail-${chapter.id}`">{{ chapter.label }}</a></nav></section>
    <section class="rd-aside-card">
      <template v-if="mode === 'comment'"><h3>交付前检查<AppIcon name="info" :size="16" /></h3><div class="rd-delivery-step"><span>PR Head 状态</span><span class="badge" :class="headTone" role="status">{{ headLabel }}</span></div><button type="button" class="btn secondary rd-full" :disabled="busy || !canCheck || !available" @click="emit('check')"><AppIcon name="search" :size="15" />{{ busy ? '正在检查…' : retryCheck ? '重试检查' : '检查 PR Head' }}</button><p class="rd-head-note">{{ headNote }}</p><div class="rd-separator" /></template>
      <template v-else><h3>{{ mode === 'user' ? '这份报告适用于' : '技术审计导出' }}</h3><p>{{ mode === 'user' ? '开发者排查、Review 讨论与审查结果留档。与 PR 直接发布内容保持不同粒度。' : '用于追溯检查行为与证据。复制和导出保留后端原始技术记录 Markdown；运行步骤与计量请看主导航“技术记录”。' }}</p></template>
      <p v-if="stale && mode !== 'comment'" class="rd-stale-note" role="status"><AppIcon name="history" :size="14" />本页内容来自旧快照，不适用于最新 Head；复制或导出前请先重新审查。</p>
      <button type="button" class="btn primary rd-full" :disabled="busy || !available" @click="emit('copy')"><AppIcon name="copy" :size="15" />{{ copied ? '已复制' : mode === 'comment' ? '复制 PR 评论' : mode === 'user' ? '复制详细报告' : '复制技术审计导出' }}</button>
      <button type="button" class="btn secondary rd-full rd-space" :disabled="busy || !available" @click="emit('download')"><AppIcon name="download" :size="15" />{{ mode === 'comment' ? '导出 Markdown' : mode === 'user' ? '导出详细报告' : '导出技术审计记录' }}</button>
      <button type="button" class="rd-text-button rd-space" :disabled="!available" @click="emit('full')">查看完整 Markdown</button>
    </section>
    <section v-if="mode === 'comment'" class="rd-aside-card"><h3>交付到 GitHub</h3><p>复制评论只写入剪贴板；直接发布需要独立授权、服务端预检和二次确认。</p><button type="button" class="btn secondary rd-full" :disabled="!canPublish" @click="emit('publish')">打开交付选项<AppIcon name="arrow-right" :size="15" /></button></section>
    <section v-else class="rd-aside-card"><h3>事实边界</h3><div class="rd-safety"><AppIcon name="info" :size="17" /><div><strong>证据支持 ≠ 动态验证</strong><small>已读测试代码不代表通过 CI</small></div></div></section>
    <p class="rd-context-tip">RepoGuardian · 证据可追溯<br />结论仅适用于本次审查快照。</p>
  </aside>
</template>
