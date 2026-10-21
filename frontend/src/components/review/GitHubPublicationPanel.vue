<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import DOMPurify from "dompurify";
import { marked } from "marked";
import type { PublicationPlan, PublicationPreflightResult, PublicationAttempt } from "../../types/publication";
import type { PublicationCapability } from "../../types/operations";
import { getPublicationCapability, preflightPublication, createPublication, getPublications, getPublication, retryPublication } from "../../api/client";
import { publicationFindingTitle } from "../../utils/reportPresentation";
import { publicationErrorText, publicationItemKindLabel, publicationItemStateLabel,
  publicationStateLabel, publicationWarningText, inlineActionLabel } from "../../utils/publicationPresentation";
import { useReviewCommentCopy } from "../../composables/useReviewCommentCopy";

const props = defineProps<{ reviewId: string; plan: PublicationPlan; capability?: PublicationCapability | null }>();
const token = ref("");
const selected = ref<string[]>([]);
const preview = ref<PublicationPreflightResult | null>(null);
const history = ref<PublicationAttempt[]>([]);
const current = ref<PublicationAttempt | null>(null);
const busy = ref(false);
const error = ref("");
const acknowledge = ref(false);
const dialog = ref<HTMLDialogElement | null>(null);
const confirmationKey = ref("");
const capability = ref<PublicationCapability | null>(props.capability ?? null);
const capabilityError = ref(false);
let controller: AbortController | null = null;
let timer: ReturnType<typeof setTimeout> | null = null;
let generation = 0;
const findings = computed(() => [...props.plan.inline_drafts, ...props.plan.summary_findings]);
const summaryHtml = computed(() => DOMPurify.sanitize(marked.parse(preview.value?.summary_markdown || "", { async: false }) as string,
  { USE_PROFILES: { html: true }, FORBID_TAGS: ["style"] }));
const canConfirm = computed(() => !!preview.value?.ready && !busy.value
  && (!preview.value.requires_partial_acknowledgement || acknowledge.value));
const publishEnabled = computed(() => capability.value?.enabled === true);
const reviewComment = computed(() => props.plan.review_comment_markdown || "");
const copy = useReviewCommentCopy({
  reviewId: () => props.reviewId, reviewedHeadSha: () => props.plan.snapshot?.head_sha ?? null,
  markdown: () => reviewComment.value, options: () => ({ detail: "compact", exclude_resolved: false }),
});
const { copied, feedback, checking, pendingCopy, previewMarkdown, copyCheckedComment, copyAcknowledged, retryPending } = copy;

function clearTimer() { if (timer) clearTimeout(timer); timer = null; }
function invalidate() { preview.value = null; acknowledge.value = false; confirmationKey.value = ""; dialog.value?.close(); }
watch(() => props.plan, () => {
  generation += 1; controller?.abort(); clearTimer(); current.value = null; history.value = []; busy.value = false;
  selected.value = findings.value.filter(item => item.default_selected !== false).map(item => item.finding_key); token.value = ""; error.value = ""; invalidate();
}, { immediate: true });
watch(selected, invalidate, { deep: true });
watch(token, () => { generation += 1; controller?.abort(); clearTimer(); busy.value = false; invalidate(); });
onMounted(() => { if (props.capability === undefined) void loadCapability(); });
onBeforeUnmount(() => { generation += 1; controller?.abort(); clearTimer(); });

async function loadCapability() {
  capabilityError.value = false;
  try { capability.value = await getPublicationCapability(); }
  catch { capabilityError.value = true; capability.value = null; }
}

async function action(operation: (signal: AbortSignal) => Promise<void>) {
  controller?.abort(); clearTimer(); controller = new AbortController();
  const signal = controller.signal, version = ++generation;
  busy.value = true; error.value = "";
  try { await operation(signal); }
  catch (failure) { if (!signal.aborted && version === generation) error.value = failure instanceof Error ? failure.message : "发布请求失败"; }
  finally { if (!signal.aborted && version === generation) busy.value = false; }
}
function schedulePoll(attempt: PublicationAttempt) {
  current.value = attempt;
  if (["queued", "preparing", "publishing"].includes(attempt.state)) timer = setTimeout(() => void refresh(attempt.id), 1500);
}
async function refresh(identity: string) {
  await action(async signal => {
    const result = await getPublication(props.reviewId, identity, token.value, signal);
    if (!signal.aborted) schedulePoll(result);
  });
}
async function preflight() {
  invalidate();
  await action(async signal => {
    const result = await preflightPublication(props.reviewId, selected.value, token.value, signal);
    if (!signal.aborted) { preview.value = result; confirmationKey.value = crypto.randomUUID(); }
  });
}
async function submit() {
  if (!canConfirm.value || !preview.value) return;
  const prepared = preview.value;
  dialog.value?.close();
  current.value = null;
  await action(async signal => {
    const accepted = await createPublication(props.reviewId, { expected_plan_hash: prepared.plan_hash, expected_head_sha: prepared.head_sha,
      publish_summary: true, finding_keys: [...selected.value], acknowledge_partial_review: acknowledge.value }, confirmationKey.value, token.value, signal);
    if (signal.aborted) return;
    const result = await getPublication(props.reviewId, accepted.publication_id, token.value, signal);
    if (!signal.aborted) schedulePoll(result);
  });
}
async function loadHistory() {
  await action(async signal => { const result = await getPublications(props.reviewId, token.value, signal); if (!signal.aborted) history.value = result; });
}
async function recover() {
  if (!current.value) return;
  const identity = current.value.id;
  await action(async signal => { const result = await retryPublication(props.reviewId, identity, token.value, signal); if (!signal.aborted) schedulePoll(result); });
}
function safeLink(value: string | null) {
  try { const url = new URL(value || ""); return url.protocol === "https:" && url.hostname === "github.com" ? url.href : undefined; }
  catch { return undefined; }
}
const placementLabel = (placement: string) => placement === "inline" ? "代码行评论" : "总结评论";
</script>

<template>
  <details class="github-publication" aria-label="GitHub 交付">
    <summary>交付到 GitHub</summary>
    <div class="github-publication__body">
      <section class="github-publication__paths" aria-label="交付方式">
        <article class="github-publication__path">
          <h3>复制评论</h3>
          <p>只把评论正文写入剪贴板，不会写入 GitHub，也不需要发布授权。粘贴到 PR 评论框即可。</p>
          <button type="button" class="button button--secondary button--compact" :disabled="checking || !reviewComment" @click="copyCheckedComment">
            {{ checking ? '正在核对 PR 版本…' : copied ? '已复制' : '复制 PR 评论' }}
          </button>
          <p class="runtime-caption" role="status">{{ feedback || (reviewComment ? '复制前会核对 PR 当前版本；仅复制评论正文，不发布。' : '当前审查结论尚无可复制的评论正文。') }}</p>
          <button v-if="pendingCopy" type="button" class="button button--secondary button--compact" @click="copyAcknowledged">{{ pendingCopy.status === 'stale' ? '仍复制带旧版本说明的评论' : '仍复制带未知状态说明的评论' }}</button>
          <details v-if="reviewComment" class="runtime-technical"><summary>预览评论正文（Markdown 原文）</summary><pre class="github-publication__copy-preview">{{ previewMarkdown }}</pre></details>
        </article>
        <article class="github-publication__path">
          <h3>直接发布到 GitHub</h3>
          <p>由服务端使用独立发布身份写入 PR：更新 PR 总结评论，并创建所选代码行评论。需要 GitHub 授权、服务端预检和二次确认。</p>
          <template v-if="publishEnabled">
            <p class="runtime-caption">发布结论与审查结论相互独立；审查版本变化后会要求重新预检。</p>
          </template>
          <template v-else-if="capability === null && !capabilityError">
            <p class="runtime-caption" role="status">正在检查服务端是否启用直接发布…</p>
          </template>
          <template v-else>
            <p class="github-publication__notice" role="status">
              {{ capabilityError ? '暂时无法确认服务端发布能力。' : '服务端未启用直接发布。' }}当前仅支持“复制评论”，由你在 GitHub 页面手动粘贴。
            </p>
            <p class="runtime-caption">启用方式：在 <code>backend/.env</code> 配置独立发布凭据、仓库授权与操作口令，再设置 <code>REPOGUARDIAN_GITHUB_PUBLISH_ENABLED=true</code> 并重启后端。</p>
            <button v-if="capabilityError" type="button" class="button button--ghost button--compact" @click="loadCapability">重新检查发布能力</button>
          </template>
        </article>
      </section>

      <template v-if="publishEnabled">
        <label class="github-publication__credential">本地发布操作口令<input v-model="token" type="password" autocomplete="off" placeholder="独立操作口令，不是 GitHub Token" :disabled="busy" /></label>
        <fieldset :disabled="busy"><legend>选择要发布的已核验问题（PR 总结评论始终发布）</legend><p v-if="!findings.length">没有合格问题草稿；预检将校验能否发布当前审查结论。</p><label v-for="finding in findings" :key="finding.finding_key"><input v-model="selected" type="checkbox" :value="finding.finding_key" />{{ publicationFindingTitle(finding) }} · {{ placementLabel(finding.placement) }}<small v-if="finding.default_selected === false">已有讨论且未发现新增信息，默认不选；可手动纳入。</small><a v-for="url in finding.discussion_urls || []" :key="url" :href="safeLink(url)" target="_blank" rel="noreferrer">查看已有讨论</a></label></fieldset>
        <div class="github-publication__actions"><button type="button" class="button button--secondary button--compact" :disabled="busy || !token" @click="preflight">检查发布条件并预览</button><button type="button" class="button button--ghost button--compact" :disabled="busy || !token" @click="loadHistory">加载发布历史</button></div>
        <p v-if="busy" role="status">正在处理发布请求…</p><p v-if="error" role="alert">{{ error }}</p>
        <section v-if="preview" aria-label="发布预检结果">
          <h3>PR 总结评论：{{ preview.summary_action === 'update' ? '将更新已有评论' : '将新建评论' }}</h3><code>审查版本 {{ preview.head_sha }}</code>
          <p>将新建 {{ preview.inline.ready }} 条代码行评论 · 已存在相同评论 {{ preview.inline.already_published }} 条 · 无法定位、将写入总结评论 {{ preview.inline.summary_fallback }} 条</p>
          <article class="markdown-body" v-html="summaryHtml" />
          <details v-for="item in preview.inline_previews" :key="item.finding_key"><summary>{{ inlineActionLabel[item.action] }} · {{ item.location?.path || '总结评论' }}<template v-if="item.location">:{{ item.location.line }} · {{ item.location.side === 'LEFT' ? 'Base' : 'Head' }}</template></summary><pre>{{ item.body_markdown }}</pre></details>
          <p v-for="warning in preview.warnings" :key="warning">{{ publicationWarningText(warning) }}</p>
          <label v-if="preview.requires_partial_acknowledgement"><input v-model="acknowledge" type="checkbox" />我已了解审查或发布不完整，以及未选择的问题不会发布，仍确认部分发布。</label>
          <button type="button" class="button button--primary button--compact" :disabled="!canConfirm" @click="dialog?.showModal()">确认发布</button>
        </section>
        <section v-if="current" aria-label="发布状态"><h3>{{ publicationStateLabel[current.state] || '状态未知' }}</h3><code>{{ current.id }}</code><p v-if="current.last_error_code">{{ publicationErrorText(current.last_error_code) }}</p>
          <ul><li v-for="item in current.items" :key="item.id">{{ publicationItemKindLabel(item.kind) }} · {{ publicationItemStateLabel[item.state] || '状态未知' }} <a v-if="safeLink(item.github_url)" :href="safeLink(item.github_url)" target="_blank" rel="noreferrer">在 GitHub 查看</a><span v-if="item.last_error_code">：{{ publicationErrorText(item.last_error_code) }}</span></li></ul>
          <button v-if="['uncertain', 'partial', 'failed'].includes(current.state)" type="button" class="button button--secondary button--compact" :disabled="busy" @click="recover">{{ current.state === 'uncertain' ? '检查远端发布状态' : '重新检查并处理剩余项目' }}</button>
          <p v-if="current.state === 'uncertain'">无法确认 GitHub 是否已收到评论，先检查远端状态；无法确认时会保持未知，需要人工处理，不会盲目重新发送。</p>
        </section>
        <section v-if="history.length" aria-label="发布历史"><button v-for="item in history" :key="item.id" type="button" class="text-button" @click="refresh(item.id)">{{ publicationStateLabel[item.state] || '状态未知' }} · {{ item.requested_at }}</button></section>
        <dialog ref="dialog" class="github-publication__dialog" @click.self="dialog?.close()"><h3>确认发布到 GitHub</h3><p>将使用服务端发布身份{{ preview?.summary_action === 'update' ? '更新' : '新建' }} PR 总结评论，并创建 {{ preview?.inline.ready }} 条新代码行评论。</p><p>审查版本：{{ preview?.head_sha }}</p><div class="github-publication__actions"><button type="button" class="button button--secondary" @click="dialog?.close()">取消</button><button type="button" class="button button--primary" :disabled="!canConfirm" @click="submit">确认发布到 GitHub</button></div></dialog>
      </template>
    </div>
  </details>
</template>

<style scoped>
.github-publication { margin: 0 0 20px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); }
.github-publication > summary { padding: 18px 24px; cursor: pointer; color: var(--primary); font-weight: 650; }
.github-publication__body { padding: 0 24px 24px; display: grid; gap: 16px; min-width: 0; font-size: 14px; line-height: 1.8; }
.github-publication__body p { margin: 0; color: var(--text-secondary); }
.github-publication__paths { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; }
.github-publication__path { display: grid; gap: 10px; align-content: start; padding: 16px; border: 1px solid var(--border); border-radius: var(--radius-sm); }
.github-publication__path h3 { margin: 0; font-size: 15px; }
.github-publication__path .button { justify-self: start; }
.github-publication__notice { padding: 10px 12px; border-radius: var(--radius-sm); background: var(--warning-soft, var(--surface-subtle)); color: var(--text-primary); }
.github-publication__credential { display: grid; gap: 8px; max-width: 480px; }
.github-publication__credential input { width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); color: var(--text-primary); }
fieldset { display: grid; gap: 10px; margin: 0; padding: 16px; border: 1px solid var(--border); border-radius: var(--radius-sm); }
label { overflow-wrap: anywhere; } input[type=checkbox] { appearance: auto; width: 16px; height: 16px; min-height: 16px; padding: 0; margin: 0 8px 0 0; vertical-align: middle; accent-color: var(--primary); }
.github-publication__actions { display: flex; flex-wrap: wrap; gap: 10px; }
section { display: grid; gap: 12px; min-width: 0; } section h3 { margin: 0; font-size: 16px; }
code, pre { overflow-wrap: anywhere; white-space: pre-wrap; } section pre, .github-publication__copy-preview { padding: 16px; background: var(--surface-subtle); font-size: 12px; }
article { padding: 18px; border: 1px solid var(--border); border-radius: var(--radius-sm); }
.github-publication__path { padding: 16px; }
.github-publication__dialog { width: min(520px, calc(100vw - 32px)); padding: 24px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); color: var(--text-primary); }
.github-publication__dialog::backdrop { background: #17232980; }
@media (max-width: 600px) { .github-publication__body { padding: 0 16px 18px; } }
</style>
