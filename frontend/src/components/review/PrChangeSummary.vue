<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { marked } from "marked";
import DOMPurify from "dompurify";
import type { ReviewTask } from "../../types/review";
import type { ReviewDigest } from "../../types/publication";
import { prChangeSummary } from "../../utils/prChangeSummary";

const props = defineProps<{ task: ReviewTask; report?: string | null; digest?: ReviewDigest | null }>();
const overview = computed(() => prChangeSummary(props.task, props.report));
const authorOpen = ref(false);
const checked = computed(() => props.digest?.examined_goals.filter(goal => goal.reason) || []);
const answered = computed(() => props.digest?.investigations?.filter(item => item.answer !== 'unresolved') || []);
const authorMarkdown = computed(() => props.task.pr?.body?.trim() || "");
const authorHtml = computed(() => DOMPurify.sanitize(marked.parse(authorMarkdown.value, { async: false }) as string,
  { USE_PROFILES: { html: true }, FORBID_TAGS: ["style", "img"] }));
watch(() => props.task.id, () => { authorOpen.value = false; });
</script>

<template>
  <section class="card card-pad rg-change-summary" aria-label="PR 变更摘要">
    <div class="card-header"><div><h2>本次 PR 做了什么</h2><p>{{ digest ? '来自后端统一审查事实；作者声明、实际变更与核验边界分别呈现' : overview.subtitle }}</p></div></div>
    <template v-if="digest">
      <p class="small rg-pr-summary"><strong>作者声明：</strong>{{ digest.author_goal ? (digest.author_goal.length > 300 ? digest.author_goal.slice(0, 300) + '…' : digest.author_goal) : '未记录作者声明' }}</p>
      <details v-if="digest.actual_changes?.length" class="space-top" open><summary>实际改动 · {{ digest.actual_changes.length }} 个文件</summary><ul class="rg-limitations"><li v-for="(change, index) in digest.actual_changes.slice(0, 5)" :key="index">{{ change }}</li></ul><p v-if="digest.actual_changes.length > 5" class="small muted">另有 {{ digest.actual_changes.length - 5 }} 个文件，详见详细报告。</p></details>
      <p v-else class="small muted space-top">未保存变更明细，无法概括实际修改。</p>
      <details class="space-top" open><summary>已检查的关键行为 · {{ checked.length }} 项记录</summary><ul v-if="checked.length" class="rg-limitations"><li v-for="(goal, index) in checked.slice(0, 3)" :key="index"><code>{{ goal.path || '文件未记录' }}{{ goal.start_line ? `:${goal.start_line}` : '' }}</code>：{{ goal.reason }}</li></ul><p v-else class="small muted">未保存具体行为检查理由，不能由覆盖数量推断。</p><p v-if="checked.length > 3" class="small muted">另有 {{ checked.length - 3 }} 项，详见详细报告。</p></details>
      <p v-if="digest.investigations_known === true" class="small muted space-top">{{ answered.length }} 项关联调查有明确答案；仅针对记录中的命题。</p>
      <p v-else class="small muted space-top">未获得有效调查记录，状态未知。</p>
      <p class="small muted space-top"><strong>目标符合性：</strong>{{ digest.intent_alignment || '未保存独立核验记录，当前未知。' }}</p>
      <details v-for="(item, index) in digest.intent_assessments || []" :key="index" class="space-top" open>
        <summary>{{ item.author_target }} · {{ ({ observed: '局部静态证据支持', contradicted: '存在静态证据矛盾', insufficient: '证据不足', not_applicable: '当前检查不适用', not_checked: '尚未核验' })[item.status] }}</summary>
        <ul><li v-for="(observation, n) in item.observations" :key="n">{{ observation.actual_behavior }}；<code>{{ observation.path }}:{{ observation.start_line || '未知' }}（{{ observation.side }}）</code>；证据 {{ observation.evidence_ids.join('、') || '未获得有效证据' }}<p v-for="(limit, k) in observation.limitations" :key="k" class="small muted">限制：{{ limit }}</p></li></ul>
      </details>
    </template>
    <p v-else class="small rg-pr-summary">{{ overview.text }}</p>
    <div class="facts">
      <span class="fact">{{ task.changed_files.length }} 个变更文件</span>
      <span class="fact">{{ task.review_units.length }} 个审查分组</span>
      <span v-if="overview.languages.length" class="fact">{{ overview.languages.join(' / ') }}</span>
    </div>
    <details v-if="authorMarkdown" class="rg-pr-original" :open="authorOpen" @toggle="authorOpen = ($event.target as HTMLDetailsElement).open">
      <summary>查看作者原始说明</summary>
      <div v-if="authorOpen" class="markdown-body rg-pr-original__body" v-html="authorHtml" />
    </details>
  </section>
</template>
