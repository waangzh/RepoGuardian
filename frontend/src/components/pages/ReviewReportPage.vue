<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { getPublicationCapability } from '../../api/client';
import type { ReviewTask } from '../../types/review';
import type { PublicationPlan } from '../../types/publication';
import type { PublicationCapability } from '../../types/operations';
import { executionErrorLabel, terminal } from '../../utils/nativeReview';
import { useReviewCommentCopy } from '../../composables/useReviewCommentCopy';
import AppIcon from '../common/AppIcon.vue';
import ReportMarkdown from '../report/ReportMarkdown.vue';
import GitHubPublicationPanel from '../review/publication/GitHubPublicationPanel.vue';
const props = defineProps<{ task: ReviewTask; plan: PublicationPlan | null; report: string; loading: boolean }>();
defineEmits<{ export: [] }>();
const mode = ref<'comment' | 'readable' | 'audit'>('comment');
const display = ref<'preview' | 'markdown'>('preview');
const showPublish = ref(false);
const deliveryResult = ref<{ tone: 'success' | 'warn' | 'muted'; text: string } | null>(null);
const capability = ref<PublicationCapability | null>(null);
const capabilityError = ref(false);
const shortHead = computed(() => props.task.snapshot.head_sha.slice(0, 8));
const commentMarkdown = computed(() => props.plan?.review_comment_markdown || '');
const copy = useReviewCommentCopy({
  reviewId: () => props.task.id, reviewedHeadSha: () => props.plan?.snapshot?.head_sha ?? null,
  markdown: () => commentMarkdown.value, options: () => ({ detail: 'compact', exclude_resolved: false }),
});
const { copied, feedback, checking, pendingCopy, previewMarkdown, headCheck, copyCheckedComment, copyAcknowledged } = copy;
const commentBody = computed(() => previewMarkdown.value || '');
const statusNotices = computed(() => {
  const notices: string[] = [];
  if (props.task.status === 'failed') notices.push('审查未正常结束：本轮记录不完整，缺失与未知不能视为通过。');
  else if (props.task.status === 'cancelled') notices.push('审查已停止：以下为停止前已保存的结果。');
  if (props.task.error) notices.push(`执行错误：${executionErrorLabel(props.task.error)}（${props.task.error}）。`);
  const outcome = props.plan?.outcome;
  if (outcome && (outcome.review_completeness !== 'complete' || outcome.coverage !== 'complete')) {
    notices.push(`覆盖不完整：${outcome.incomplete_investigations} 项调查未完成，${outcome.pending_limitation_count} 项限制待复核。`);
  }
  if (headCheck.value?.status === 'stale') notices.push(`PR 已更新：最新提交 ${headCheck.value.latest_head_sha?.slice(0, 8) || '未知'}，本轮结论仅适用于审查快照。`);
  else if (headCheck.value?.status === 'unknown') notices.push('暂时无法确认 PR 是否有新提交，本轮记录不能作为当前版本结论。');
  return notices;
});
async function loadCapability() {
  capabilityError.value = false;
  try { capability.value = await getPublicationCapability(); }
  catch { capabilityError.value = true; capability.value = null; }
}
onMounted(() => { void loadCapability(); });
</script>
<template>
  <div class="rd-workspace">
    <div class="rd-heading">
      <div><h2>审查意见</h2><p>基于提交 <code>{{ shortHead }}</code> · 已保存的本轮审查结果</p></div>
      <details class="rd-snapshot-info"><summary>快照与交付边界</summary>
        <dl><div><dt>HEAD</dt><dd><code>{{ task.snapshot.head_sha }}</code></dd></div><div><dt>有效 BASE</dt><dd><code>{{ task.snapshot.effective_base_sha }}</code></dd></div></dl>
        <p>导出保留原快照，不表示已核对最新 PR。复制会再次检查 Head；直接发布需要预检及二次确认。未执行目标仓库代码、测试或构建。</p>
      </details>
    </div>
    <div class="rd-shell"><div class="rd-shell-top"><nav class="rd-tabs" aria-label="审查意见视图"><button v-for="item in ([['comment','审查意见'],['readable','详细报告'],['audit','技术记录']] as const)" :key="item[0]" :class="{ active: mode === item[0] }" :aria-current="mode === item[0] ? 'page' : undefined" @click="mode = item[0]">{{ item[1] }}</button></nav><small class="rd-shell-caption">固定快照记录</small></div>
      <main class="rd-main">
        <p v-if="loading" role="status">正在读取审查意见…</p>
        <p v-else-if="!plan" class="muted">审查尚未形成可读取的交付计划。</p>
        <template v-else-if="mode === 'comment'">
          <div class="rd-toolbar">
            <div class="segmented" role="group" aria-label="正文显示模式"><button :class="{ active: display === 'preview' }" @click="display = 'preview'">预览</button><button :class="{ active: display === 'markdown' }" @click="display = 'markdown'">Markdown</button></div>
            <div class="rd-toolbar-actions">
              <span v-if="deliveryResult" class="rd-delivery-result" :data-tone="deliveryResult.tone" role="status">{{ deliveryResult.text }}</span>
              <button class="btn secondary" :disabled="checking || !commentMarkdown" @click="copyCheckedComment"><AppIcon name="copy" />{{ checking ? '正在核对 PR 版本…' : copied ? '已复制' : '复制完整 PR 评论' }}</button>
              <button class="btn secondary" :aria-expanded="showPublish" @click="showPublish = !showPublish"><AppIcon name="send" />{{ showPublish ? '收起发布面板' : '发布到 GitHub' }}</button>
            </div>
          </div>
          <p v-if="capability && !capability.enabled" class="rd-toolbar-note">服务端未启用直接发布，评论仍可复制后手动粘贴。</p>
          <p v-if="feedback || pendingCopy" class="runtime-caption rd-copy-feedback" role="status">{{ feedback }}<button v-if="pendingCopy" type="button" class="button button--secondary button--compact" @click="copyAcknowledged">{{ pendingCopy.status === 'stale' ? '仍复制带旧版本说明的评论' : '仍复制带未知状态说明的评论' }}</button></p>
          <div v-for="notice in statusNotices" :key="notice" class="rd-notice warn" role="alert"><AppIcon name="alert" :size="16" /><span>{{ notice }}</span></div>
          <p v-if="!commentMarkdown" class="muted">当前审查结论尚无可复制的评论正文。</p>
          <section v-else class="rd-comment" aria-label="完整 PR 评论">
            <div class="rd-comment-header"><div><strong>完整 PR 评论</strong><small>与“复制完整 PR 评论”使用同一正文 · 基于提交 {{ shortHead }}</small></div><span class="badge">{{ display === 'preview' ? '预览' : 'Markdown' }}</span></div>
            <div v-if="display === 'preview'" class="rd-pr-comment"><ReportMarkdown :content="commentBody" /></div>
            <pre v-else class="rd-comment-source">{{ commentBody }}</pre>
          </section>
          <GitHubPublicationPanel v-if="showPublish && terminal(task.status)" :review-id="task.id" :plan="plan" :capability="capability" :capability-error="capabilityError" @result="deliveryResult = $event" @retry-capability="loadCapability" />
        </template>
        <template v-else-if="mode === 'readable'">
          <div class="rd-toolbar"><span class="rd-toolbar-note">本轮审查的完整报告，包含变更理解、问题证据与限制。</span><div class="rd-toolbar-actions"><button class="btn secondary" @click="$emit('export')"><AppIcon name="download" />导出详细报告</button></div></div>
          <ReportMarkdown :content="plan.user_report_markdown" />
        </template>
        <template v-else><p class="notice small">结构化图执行记录来自本任务，包含候选、核验及覆盖；不包含共享消息历史。</p><pre v-if="task.review_report" class="rg-json space-top">{{ JSON.stringify(task.review_report,null,2) }}</pre><p v-else class="muted space-top">尚无已保存的图执行记录。</p><details v-if="report" class="space-top"><summary class="link">完整归档报告</summary><ReportMarkdown :content="report" /></details></template>
      </main>
    </div>
  </div>
</template>
