<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import DOMPurify from "dompurify";
import { marked } from "marked";
import type { PublicationPlan, PublicationPreflightResult, PublicationAttempt, PublicationFinding } from "../../../types/publication";
import type { PublicationCapability } from "../../../types/operations";
import { preflightPublication, createPublication, getPublications, getPublication, retryPublication } from "../../../api/client";
import { publicationFindingTitle } from "../../../utils/reportPresentation";
import { publicationErrorText, publicationItemKindLabel, publicationItemStateLabel,
  publicationStateLabel, publicationWarningText, inlineActionLabel,
  summarySourceLabel } from "../../../utils/publicationPresentation";
import StatusBadge from "../../common/StatusBadge.vue";

const props = defineProps<{ reviewId: string; plan: PublicationPlan; capability: PublicationCapability | null; capabilityError: boolean }>();
const emit = defineEmits<{
  result: [value: { tone: "success" | "warn" | "muted"; text: string } | null];
  "retry-capability": [];
}>();
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
const expandAll = ref(false);
let controller: AbortController | null = null;
let timer: ReturnType<typeof setTimeout> | null = null;
let generation = 0;
const findings = computed(() => props.plan.presentation?.findings
  ?? [...props.plan.inline_drafts, ...props.plan.summary_findings].sort((left, right) =>
    ["critical", "high", "medium", "low"].indexOf(left.severity) - ["critical", "high", "medium", "low"].indexOf(right.severity)
    || (left.anchor?.file_path ?? "").localeCompare(right.anchor?.file_path ?? "")
    || (left.anchor?.start_line ?? 0) - (right.anchor?.start_line ?? 0)));
const findingByKey = computed(() => new Map(findings.value.map(item => [item.finding_key, item])));
const severityOf = (key: string): PublicationFinding["severity"] => findingByKey.value.get(key)?.severity ?? "low";
const summaryHtml = computed(() => renderMarkdown(preview.value?.summary_markdown || ""));
const canConfirm = computed(() => !!preview.value?.ready && !busy.value
  && (!preview.value.requires_partial_acknowledgement || acknowledge.value));
const publishEnabled = computed(() => props.capability?.enabled === true);

function renderMarkdown(value: string) {
  return DOMPurify.sanitize(marked.parse(value, { async: false, gfm: true }) as string,
    { USE_PROFILES: { html: true }, FORBID_TAGS: ["style"] });
}
function clearTimer() { if (timer) clearTimeout(timer); timer = null; }
function invalidate() { preview.value = null; acknowledge.value = false; confirmationKey.value = ""; dialog.value?.close(); }
watch(() => props.plan, (value, previous) => {
  generation += 1; controller?.abort(); clearTimer(); current.value = null; history.value = []; busy.value = false;
  selected.value = findings.value.map(item => item.finding_key); token.value = ""; error.value = ""; expandAll.value = false;
  invalidate(); if (previous !== undefined) emit("result", null);
}, { immediate: true });
watch(selected, invalidate, { deep: true });
watch(token, () => { generation += 1; controller?.abort(); clearTimer(); busy.value = false; invalidate(); });
watch(current, value => {
  if (!value) { emit("result", null); return; }
  if (value.state === "succeeded") {
    const inline = value.items.filter(item => item.kind === "inline" && item.state === "published").length;
    emit("result", { tone: "success", text: inline ? `已发布总结及 ${inline} 条代码意见` : "已发布总结评论" });
  } else if (["partial", "failed", "uncertain", "stale"].includes(value.state)) {
    emit("result", { tone: "warn", text: publicationStateLabel[value.state] });
  } else emit("result", { tone: "muted", text: publicationStateLabel[value.state] });
});
onBeforeUnmount(() => { generation += 1; controller?.abort(); clearTimer(); });

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
const highPriority = (key: string) => ["critical", "high"].includes(severityOf(key));
</script>

<template>
  <section class="github-publication" aria-label="发布到 GitHub">
    <header class="github-publication__head"><h3>发布到 GitHub</h3><p>由服务端使用独立发布身份写入 PR：更新总结评论并创建所选代码行评论。复制评论不受此限制。</p></header>
    <template v-if="publishEnabled">
      <fieldset :disabled="busy"><legend>1. 选择要发布的已核验问题（PR 总结评论始终发布）</legend>
        <p v-if="!findings.length">没有合格问题草稿；预检将校验能否发布当前审查结论。</p>
        <label v-for="finding in findings" :key="finding.finding_key"><input v-model="selected" type="checkbox" :value="finding.finding_key" /><span>{{ publicationFindingTitle(finding) }}</span><StatusBadge :status="finding.severity" /><span class="github-publication__placement">{{ placementLabel(finding.placement) }}</span></label>
      </fieldset>
      <div class="github-publication__authorize"><label class="github-publication__credential">2. 发布授权<input v-model="token" type="password" autocomplete="off" placeholder="独立操作口令，不是 GitHub Token" :disabled="busy" /></label><button type="button" class="button button--secondary button--compact" :disabled="busy || !token" @click="preflight">检查发布条件并预览</button></div>
      <p v-if="busy" role="status">正在处理发布请求…</p><p v-if="error" role="alert">{{ error }}</p>
      <section v-if="preview" aria-label="实际发布预览">
        <h3>3. 实际发布预览</h3>
        <p>PR 总结评论{{ preview.summary_action === 'update' ? '将更新已有评论' : '将新建评论' }} · 审查版本 <code>{{ preview.head_sha.slice(0, 8) }}</code></p>
        <p>将新建 {{ preview.inline.ready }} 条代码行评论 · 已存在相同评论 {{ preview.inline.already_published }} 条 · 无法定位、将写入总结评论 {{ preview.inline.summary_fallback }} 条</p>
        <p v-if="preview.presentation">已确认 {{ preview.presentation.delivery.confirmed }} 项 / 可交付 {{ preview.presentation.delivery.eligible }} 项 / 选择 {{ preview.presentation.delivery.selected }} 项 / 仅在总结中反馈 {{ preview.presentation.delivery.summary_only }} 项 / 未选择 {{ preview.presentation.delivery.not_selected }} 项。</p>
        <p v-if="preview.presentation">{{ summarySourceLabel(preview.presentation.summary_source) }}；当前是发布预览，实际成功状态与讨论链接以 GitHub 回执为准。</p>
        <article class="markdown-body github-publication__summary" v-html="summaryHtml" />
        <div class="github-publication__inline-head"><h4>逐条代码意见</h4><button v-if="preview.inline_previews.length" type="button" class="text-button" @click="expandAll = !expandAll">{{ expandAll ? '收起全部' : '展开全部' }}</button></div>
        <details v-for="item in preview.inline_previews" :key="item.finding_key" class="github-publication__inline" :open="expandAll || highPriority(item.finding_key)">
          <summary><span class="github-publication__inline-location"><code>{{ item.location?.path || '总结评论' }}</code><template v-if="item.location">:{{ item.location.line }} · {{ item.location.side === 'LEFT' ? 'Base' : 'Head' }}</template></span><StatusBadge :status="severityOf(item.finding_key)" /><span class="github-publication__inline-action">{{ inlineActionLabel[item.action] }}</span></summary>
          <article class="markdown-body" v-html="renderMarkdown(item.body_markdown)" />
        </details>
        <p v-for="warning in preview.warnings" :key="warning">{{ publicationWarningText(warning) }}</p>
        <label v-if="preview.requires_partial_acknowledgement" class="github-publication__acknowledge"><input v-model="acknowledge" type="checkbox" />我已了解审查或发布不完整，以及未选择的问题不会发布，仍确认部分发布。</label>
        <button type="button" class="button button--primary button--compact" :disabled="!canConfirm" @click="dialog?.showModal()">确认发布</button>
      </section>
      <section v-if="current" aria-label="发布结果">
        <h3>4. 发布结果：{{ publicationStateLabel[current.state] || '状态未知' }}</h3>
        <p v-if="current.last_error_code">{{ publicationErrorText(current.last_error_code) }}</p>
        <button v-if="['uncertain', 'partial', 'failed'].includes(current.state)" type="button" class="button button--secondary button--compact" :disabled="busy" @click="recover">{{ current.state === 'uncertain' ? '检查远端发布状态' : '重新检查并处理剩余项目' }}</button>
        <p v-if="current.state === 'uncertain'">无法确认 GitHub 是否已收到评论，先检查远端状态；无法确认时会保持未知，需要人工处理，不会盲目重新发送。</p>
      </section>
      <details class="github-publication__record">
        <summary>交付记录</summary>
        <div class="github-publication__record-body">
          <button type="button" class="button button--ghost button--compact" :disabled="busy || !token" @click="loadHistory">刷新交付记录</button>
          <ul v-if="current"><li v-for="item in current.items" :key="item.id">{{ publicationItemKindLabel(item.kind) }} · {{ publicationItemStateLabel[item.state] || '状态未知' }} <a v-if="safeLink(item.github_url)" :href="safeLink(item.github_url)" target="_blank" rel="noreferrer">在 GitHub 查看</a><span v-if="item.last_error_code">：{{ publicationErrorText(item.last_error_code) }}</span></li></ul>
          <ul v-if="history.length"><li v-for="item in history" :key="item.id"><button type="button" class="text-button" @click="refresh(item.id)">{{ publicationStateLabel[item.state] || '状态未知' }} · {{ item.requested_at }}</button></li></ul>
          <p v-if="!current && !history.length" class="runtime-caption">尚无交付记录。发布后可在此查看每次投递的条目与远端链接。</p>
        </div>
      </details>
      <dialog ref="dialog" class="github-publication__dialog" @click.self="dialog?.close()"><h3>确认发布到 GitHub</h3><p>将使用服务端发布身份{{ preview?.summary_action === 'update' ? '更新' : '新建' }} PR 总结评论，并创建 {{ preview?.inline.ready }} 条新代码行评论。</p><p>审查版本：{{ preview?.head_sha }}</p><div class="github-publication__actions"><button type="button" class="button button--secondary" @click="dialog?.close()">取消</button><button type="button" class="button button--primary" :disabled="!canConfirm" @click="submit">确认发布到 GitHub</button></div></dialog>
    </template>
    <template v-else-if="capability === null && !capabilityError"><p class="runtime-caption" role="status">正在检查服务端是否启用直接发布…</p></template>
    <template v-else>
      <p class="github-publication__notice" role="status">{{ capabilityError ? '暂时无法确认服务端发布能力。' : '服务端未启用直接发布。' }}完整评论仍可在上方阅读和复制，粘贴到 GitHub 即可。</p>
      <button v-if="capabilityError" type="button" class="button button--ghost button--compact" @click="$emit('retry-capability')">重新检查发布能力</button>
      <details class="runtime-technical"><summary>如何启用直接发布</summary><p>在 <code>backend/.env</code> 配置独立发布凭据、仓库授权与操作口令，再设置 <code>REPOGUARDIAN_GITHUB_PUBLISH_ENABLED=true</code> 并重启后端。</p></details>
    </template>
  </section>
</template>

<style scoped>
.github-publication { margin: 20px 0 0; padding: 20px 22px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); display: grid; gap: 16px; min-width: 0; font-size: 14px; line-height: 1.8; }
.github-publication__head h3 { margin: 0; font-size: 16px; }
.github-publication__head p { margin: 4px 0 0; color: var(--text-secondary); font-size: 13px; }
.github-publication__placement { color: var(--text-secondary); font-size: 12px; }
.github-publication__authorize { display: flex; flex-wrap: wrap; align-items: end; gap: 12px; }
.github-publication__credential { display: grid; gap: 8px; flex: 1 1 320px; max-width: 480px; font-size: 13px; }
.github-publication__credential input { width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); color: var(--text-primary); }
.github-publication__notice { padding: 10px 12px; border-radius: var(--radius-control); background: var(--warning-soft, var(--surface-subtle)); color: var(--warning); margin: 0; }
.github-publication fieldset { display: grid; gap: 10px; margin: 0; padding: 16px; border: 1px solid var(--border); border-radius: var(--radius-sm); }
.github-publication legend { padding: 0 6px; font-weight: 650; font-size: 13px; }
.github-publication label { overflow-wrap: anywhere; }
.github-publication input[type=checkbox] { appearance: auto; width: 16px; height: 16px; min-height: 16px; padding: 0; margin: 0 8px 0 0; vertical-align: middle; accent-color: var(--primary); }
.github-publication__acknowledge { display: flex; align-items: start; gap: 4px; }
.github-publication__actions { display: flex; flex-wrap: wrap; gap: 10px; }
.github-publication section { display: grid; gap: 12px; min-width: 0; }
.github-publication section h3 { margin: 0; font-size: 16px; }
.github-publication code, .github-publication pre { overflow-wrap: anywhere; white-space: pre-wrap; }
.github-publication__summary { padding: 16px; border: 1px solid var(--border); border-radius: var(--radius-control); }
.github-publication__inline-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.github-publication__inline-head h4 { margin: 0; font-size: 14px; }
.github-publication__inline { border: 1px solid var(--border); border-radius: var(--radius-control); padding: 12px 14px; }
.github-publication__inline summary { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; cursor: pointer; }
.github-publication__inline-location { font-size: 13px; overflow-wrap: anywhere; }
.github-publication__inline-action { color: var(--text-secondary); font-size: 12px; }
.github-publication__inline article { margin-top: 12px; }
.github-publication__record { border-top: 1px solid var(--border); padding-top: 12px; }
.github-publication__record > summary { cursor: pointer; color: var(--ink); font-weight: 650; font-size: 13px; }
.github-publication__record-body { display: grid; gap: 10px; margin-top: 10px; font-size: 13px; }
.github-publication__record ul { margin: 0; padding-left: 18px; color: var(--text-secondary); display: grid; gap: 6px; }
.github-publication__dialog { width: min(520px, calc(100vw - 32px)); padding: 24px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); color: var(--text-primary); }
.github-publication__dialog::backdrop { background: #17232980; }
@media (max-width: 600px) { .github-publication { padding: 16px; } }
</style>
