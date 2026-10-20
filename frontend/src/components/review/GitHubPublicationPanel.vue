<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import DOMPurify from "dompurify";
import { marked } from "marked";
import type { PublicationPlan, PublicationPreflightResult, PublicationAttempt } from "../../types/publication";
import { preflightPublication, createPublication, getPublications, getPublication, retryPublication } from "../../api/client";
import { publicationFindingTitle } from "../../utils/reportPresentation";

const props = defineProps<{ reviewId: string; plan: PublicationPlan }>();
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
let controller: AbortController | null = null;
let timer: ReturnType<typeof setTimeout> | null = null;
let generation = 0;
const findings = computed(() => [...props.plan.inline_drafts, ...props.plan.summary_findings]);
const summaryHtml = computed(() => DOMPurify.sanitize(marked.parse(preview.value?.summary_markdown || "", { async: false }) as string,
  { USE_PROFILES: { html: true }, FORBID_TAGS: ["style"] }));
const canConfirm = computed(() => !!preview.value?.ready && !busy.value
  && (!preview.value.requires_partial_acknowledgement || acknowledge.value));
const labels: Record<string, string> = { queued: "已入队", preparing: "正在预检", publishing: "正在投递", succeeded: "发布已确认",
  partial: "部分发布", failed: "发布失败", uncertain: "结果未知，需检查远端", stale: "审查版本已过期",
  pending: "待投递", published: "已确认投递", summary_only: "已降为 Summary-only" };
function clearTimer() { if (timer) clearTimeout(timer); timer = null; }
function invalidate() { preview.value = null; acknowledge.value = false; confirmationKey.value = ""; dialog.value?.close(); }
watch(() => props.plan, () => {
  generation += 1; controller?.abort(); clearTimer(); current.value = null; history.value = []; busy.value = false;
  selected.value = findings.value.filter(item => item.default_selected !== false).map(item => item.finding_key); token.value = ""; error.value = ""; invalidate();
}, { immediate: true });
watch(selected, invalidate, { deep: true });
watch(token, () => { generation += 1; controller?.abort(); clearTimer(); busy.value = false; invalidate(); });
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
</script>

<template>
  <details class="github-publication" aria-label="GitHub 手动发布">
    <summary>发布到 GitHub</summary>
    <div class="github-publication__body">
      <p>由独立发布身份更新 PR 主摘要，并创建所选行级评论。需要先完成预检，再确认写入；审查结论保持不变。</p>
      <label class="github-publication__credential">本地发布操作口令<input v-model="token" type="password" autocomplete="off" placeholder="独立操作口令，不是 GitHub Token" :disabled="busy" /></label>
      <fieldset :disabled="busy"><legend>选择已核验问题（Summary 始终发布）</legend><p v-if="!findings.length">没有合格问题草稿；预检将校验能否发布当前审查结论。</p><label v-for="finding in findings" :key="finding.finding_key"><input v-model="selected" type="checkbox" :value="finding.finding_key" />{{ publicationFindingTitle(finding) }} · {{ finding.placement }}<small v-if="finding.default_selected === false">已有讨论且未发现新增信息，默认不选；可手动纳入。</small><a v-for="url in finding.discussion_urls || []" :key="url" :href="safeLink(url)" target="_blank" rel="noreferrer">查看已有讨论</a></label></fieldset>
      <div class="github-publication__actions"><button type="button" class="button button--secondary button--compact" :disabled="busy || !token" @click="preflight">预检并预览</button><button type="button" class="button button--ghost button--compact" :disabled="busy || !token" @click="loadHistory">加载发布历史</button></div>
      <p v-if="busy" role="status">正在处理发布请求…</p><p v-if="error" role="alert">{{ error }}</p>
      <section v-if="preview" aria-label="发布预检结果">
        <h3>Summary：{{ preview.summary_action === 'create' ? '创建' : '更新' }}</h3><code>Head {{ preview.head_sha }}</code>
        <p>新建 Inline {{ preview.inline.ready }} · 已存在 {{ preview.inline.already_published }} · 降为 Summary-only {{ preview.inline.summary_fallback }}</p>
        <article class="markdown-body" v-html="summaryHtml" />
        <details v-for="item in preview.inline_previews" :key="item.finding_key"><summary>{{ item.action }} · {{ item.location?.path || 'Summary-only' }}<template v-if="item.location">:{{ item.location.line }} · {{ item.location.side }}</template></summary><pre>{{ item.body_markdown }}</pre></details>
        <p v-for="warning in preview.warnings" :key="warning">{{ warning }}</p>
        <label v-if="preview.requires_partial_acknowledgement"><input v-model="acknowledge" type="checkbox" />我已了解审查或投递不完整，以及未选择的问题不会投递，仍确认部分发布。</label>
        <button type="button" class="button button--primary button--compact" :disabled="!canConfirm" @click="dialog?.showModal()">确认选择并发布</button>
      </section>
      <section v-if="current" aria-label="发布状态"><h3>{{ labels[current.state] || current.state }}</h3><code>{{ current.id }}</code><p v-if="current.last_error_code">{{ current.last_error_code }}</p>
        <ul><li v-for="item in current.items" :key="item.id">{{ item.kind }} · {{ labels[item.state] || item.state }} <a v-if="safeLink(item.github_url)" :href="safeLink(item.github_url)" target="_blank" rel="noreferrer">在 GitHub 查看</a><code v-if="item.last_error_code">{{ item.last_error_code }}</code></li></ul>
        <button v-if="['uncertain', 'partial', 'failed'].includes(current.state)" type="button" class="button button--secondary button--compact" :disabled="busy" @click="recover">{{ current.state === 'uncertain' ? '检查远端发布状态' : '重新检查并处理剩余项目' }}</button>
        <p v-if="current.state === 'uncertain'">未知结果会先对账；无法确认时保持未知，需要人工处理，不会盲目重新发送。</p>
      </section>
      <section v-if="history.length" aria-label="发布历史"><button v-for="item in history" :key="item.id" type="button" class="text-button" @click="refresh(item.id)">{{ labels[item.state] || item.state }} · {{ item.requested_at }}</button></section>
      <dialog ref="dialog" class="github-publication__dialog" @click.self="dialog?.close()"><h3>确认写入 GitHub</h3><p>将使用服务端发布身份{{ preview?.summary_action === 'update' ? '更新' : '创建' }} PR 主摘要，并创建 {{ preview?.inline.ready }} 条新行级评论。</p><p>Head：{{ preview?.head_sha }}</p><div class="github-publication__actions"><button type="button" class="button button--secondary" @click="dialog?.close()">取消</button><button type="button" class="button button--primary" :disabled="!canConfirm" @click="submit">确认写入 GitHub</button></div></dialog>
    </div>
  </details>
</template>

<style scoped>
.github-publication { margin: 0 0 20px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); }
.github-publication > summary { padding: 18px 24px; cursor: pointer; color: var(--primary); font-weight: 650; }
.github-publication__body { padding: 0 24px 24px; display: grid; gap: 16px; min-width: 0; font-size: 14px; line-height: 1.8; }
.github-publication__body p { margin: 0; color: var(--text-secondary); }
.github-publication__credential { display: grid; gap: 8px; max-width: 480px; }
.github-publication__credential input { width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); color: var(--text-primary); }
fieldset { display: grid; gap: 10px; margin: 0; padding: 16px; border: 1px solid var(--border); border-radius: var(--radius-sm); }
label { overflow-wrap: anywhere; } input[type=checkbox] { appearance: auto; width: 16px; height: 16px; min-height: 16px; padding: 0; margin: 0 8px 0 0; vertical-align: middle; accent-color: var(--primary); }
.github-publication__actions { display: flex; flex-wrap: wrap; gap: 10px; }
section { display: grid; gap: 12px; min-width: 0; } section h3 { margin: 0; font-size: 16px; }
code, pre { overflow-wrap: anywhere; white-space: pre-wrap; } section pre { padding: 16px; background: var(--surface-subtle); font-size: 12px; }
article { padding: 18px; border: 1px solid var(--border); border-radius: var(--radius-sm); }
.github-publication__dialog { width: min(520px, calc(100vw - 32px)); padding: 24px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); color: var(--text-primary); }
.github-publication__dialog::backdrop { background: #17232980; }
@media (max-width: 600px) { .github-publication__body { padding: 0 16px 18px; } }
</style>
