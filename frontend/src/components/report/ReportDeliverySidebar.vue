<script setup lang="ts">
import type { ReportMode } from './ReportModeTabs.vue';
import AppIcon from '../common/AppIcon.vue';
defineProps<{ mode: ReportMode; detail: 'compact' | 'full'; excludeResolved: boolean; available: boolean;
  busy: boolean; copied: boolean; headLabel: string; headTone: string; headNote: string; canCheck: boolean; canPublish: boolean }>();
const emit = defineEmits<{ 'update:detail': [value: 'compact' | 'full']; 'update:excludeResolved': [value: boolean];
  copy: []; download: []; check: []; publish: []; full: [] }>();
const chapters = [{ id: 'conclusion', label: '审查结论' }, { id: 'changes', label: '01 · 变更理解' },
  { id: 'evidence', label: '02 · 代码依据' }, { id: 'gaps', label: '03 · 已知局限' }, { id: 'actions', label: '04 · 后续建议' }];
</script>
<template>
  <aside class="rd-aside" aria-label="报告工具区">
    <section v-if="mode === 'comment'" class="rd-aside-card">
      <h3>交付设置<AppIcon name="settings" :size="16" /></h3><p>控制复制到 PR 的信息密度，而不是改变机器审查结论。</p>
      <label class="rd-field">评论内容密度<select :value="detail" :disabled="!canCheck" aria-label="评论内容密度" @change="emit('update:detail', ($event.target as HTMLSelectElement).value as 'compact' | 'full')"><option value="compact">精简版 · 保留全部问题正文</option><option value="full">完整版 · 附加完整范围信息</option></select></label>
      <label class="rd-checkrow"><input :checked="excludeResolved" :disabled="!canCheck" type="checkbox" @change="emit('update:excludeResolved', ($event.target as HTMLInputElement).checked)" /><span>排除已处理的非阻断事项<small>不会隐藏阻断问题</small></span></label>
      <div class="rd-safety"><AppIcon name="shield" :size="17" /><div><strong>不会改变审查事实</strong><small>评论选项仅调整交付内容</small></div></div>
    </section>
    <section v-if="mode === 'user'" class="rd-aside-card"><h3>章节导航</h3><nav class="rd-toc" aria-label="详细报告章节"><a v-for="chapter in chapters" :key="chapter.id" :href="`#rd-detail-${chapter.id}`">{{ chapter.label }}</a></nav></section>
    <section class="rd-aside-card">
      <template v-if="mode === 'comment'"><h3>交付前检查<AppIcon name="info" :size="16" /></h3><div class="rd-delivery-step"><span>PR Head 状态</span><span class="badge" :class="headTone" role="status">{{ headLabel }}</span></div><button type="button" class="btn secondary rd-full" :disabled="busy || !canCheck || !available" @click="emit('check')"><AppIcon name="search" :size="15" />{{ busy ? '正在检查…' : '检查 PR Head' }}</button><p class="rd-head-note">{{ headNote }}</p><div class="rd-separator" /></template>
      <template v-else><h3>{{ mode === 'user' ? '这份报告适用于' : '审计导出' }}</h3><p>{{ mode === 'user' ? '开发者排查、Review 讨论与审查结果留档。与 PR 直接发布内容保持不同粒度。' : '用于追溯检查行为与证据。复制和导出保留后端原始审计 Markdown。' }}</p></template>
      <button type="button" class="btn primary rd-full" :disabled="busy || !available" @click="emit('copy')"><AppIcon name="copy" :size="15" />{{ copied ? '已复制' : mode === 'comment' ? '复制 PR 评论' : mode === 'user' ? '复制详细报告' : '复制技术审计' }}</button>
      <button type="button" class="btn secondary rd-full rd-space" :disabled="busy || !available" @click="emit('download')"><AppIcon name="download" :size="15" />{{ mode === 'comment' ? '导出 Markdown' : mode === 'user' ? '导出详细报告' : '导出技术审计' }}</button>
      <button type="button" class="rd-text-button rd-space" :disabled="!available" @click="emit('full')">查看完整 Markdown</button>
    </section>
    <section v-if="mode === 'comment'" class="rd-aside-card"><h3>发布到 GitHub</h3><p>正式发布需要独立授权、服务端预检和二次确认。</p><button type="button" class="btn secondary rd-full" :disabled="!canPublish" @click="emit('publish')">查看发布流程<AppIcon name="arrow-right" :size="15" /></button></section>
    <section v-else class="rd-aside-card"><h3>事实边界</h3><div class="rd-safety"><AppIcon name="info" :size="17" /><div><strong>证据支持 ≠ 动态验证</strong><small>已读测试代码不代表通过 CI</small></div></div></section>
    <p class="rd-context-tip">RepoGuardian · 证据可追溯<br />结论仅适用于本次审查快照。</p>
  </aside>
</template>
