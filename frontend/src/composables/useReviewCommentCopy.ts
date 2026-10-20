import { onScopeDispose, ref, watch } from "vue";
import { checkReviewComment } from "../api/client";
import type { ReviewCommentCheck, ReviewCommentOptions } from "../types/publication";

interface CommentSource {
  reviewId: () => string | undefined;
  reviewedHeadSha: () => string | null | undefined;
  markdown: () => string | null | undefined;
  options: () => ReviewCommentOptions;
}

export function useReviewCommentCopy(source: CommentSource, load = checkReviewComment) {
  const copied = ref(false);
  const feedback = ref("");
  const checking = ref(false);
  const pendingCopy = ref<ReviewCommentCheck | null>(null);
  const headCheck = ref<ReviewCommentCheck | null>(null);
  let pendingAction: ((content: string) => Promise<void> | void) | null = null;
  let controller: AbortController | null = null;
  let revision = 0;

  function resetCopy() {
    revision += 1;
    controller?.abort(); controller = null;
    checking.value = false; pendingCopy.value = null; pendingAction = null; headCheck.value = null; copied.value = false; feedback.value = "";
  }
  watch([source.reviewId, source.reviewedHeadSha, source.markdown, () => source.options().detail,
    () => source.options().exclude_resolved, () => source.options().include_discussed,
    () => source.options().finding_key], resetCopy, { flush: "sync" });
  onScopeDispose(resetCopy);

  async function writeClipboard(content: string) {
    const current = revision;
    try {
      await navigator.clipboard.writeText(content);
      if (current !== revision) return;
      copied.value = true; feedback.value = "当前评论或报告已复制。";
    } catch {
      if (current === revision) { copied.value = false; feedback.value = "复制失败，请重试或导出 Markdown 文件。"; }
    }
  }

  async function checkedComment(onReady: (content: string) => Promise<void> | void, checkOnly = false) {
    if (!source.markdown() || checking.value) return;
    const id = source.reviewId();
    const head = source.reviewedHeadSha() ?? null;
    const options = { ...source.options() };
    if (!id) { feedback.value = "缺少审查身份，无法检查复制快照。"; return; }
    resetCopy();
    const request = new AbortController();
    controller = request; checking.value = true; feedback.value = "正在检查远端 Head…";
    try {
      const result = await load(id, request.signal, options);
      if (request.signal.aborted || controller !== request) return;
      if (result.review_id !== id || result.reviewed_head_sha !== head
          || !["current", "stale", "unknown"].includes(result.status)
          || typeof result.review_comment_markdown !== "string" || !result.review_comment_markdown
          || result.detail !== options.detail
          || !Array.isArray(result.included_finding_keys) || !Array.isArray(result.omitted_finding_keys)
          || !Array.isArray(result.retained_blocking_keys)
          || (options.finding_key && (result.included_finding_keys?.length !== 1 || result.included_finding_keys[0] !== options.finding_key))
          || (result.status === "current" && (!head || result.latest_head_sha !== head))) throw new Error("快照或所选问题不匹配");
      headCheck.value = result;
      if (checkOnly) {
        feedback.value = result.status === 'current' ? 'PR Head 与本次审查快照一致；交付时将再次检查。'
          : result.status === 'stale' ? 'PR 已更新，本轮结论不适用于最新提交；建议重新审查。' : '暂时无法确认 PR 是否有新提交。请点击“重试检查”；本轮记录不能作为当前版本结论。';
      } else if (result.status === "current") await onReady(result.review_comment_markdown);
      else {
        pendingCopy.value = result;
        pendingAction = onReady;
        feedback.value = result.status === "stale"
          ? "PR 已更新，本轮结论不适用于最新提交。请重新审查，或明确复制带旧快照说明的历史记录。"
          : "暂时无法确认 PR 是否有新提交，无法判断评论是否过期。请点击“重试检查”，或明确复制带状态说明的历史记录。";
      }
    } catch { if (!request.signal.aborted) feedback.value = "快照检查失败或所选问题已不可复制，请重试；尚未复制。"; }
    finally { if (controller === request) { checking.value = false; controller = null; } }
  }

  async function copyCheckedComment() {
    await checkedComment(writeClipboard);
  }
  async function retryPending() {
    const action = pendingAction;
    if (action) await checkedComment(action);
  }

  async function copyAcknowledged() {
    const result = pendingCopy.value;
    const action = pendingAction;
    pendingCopy.value = null;
    pendingAction = null;
    if (result && action) await action(result.review_comment_markdown);
  }
  function cancelPending() { pendingCopy.value = null; pendingAction = null; }
  const checkHead = () => checkedComment(() => {}, true);
  return { copied, feedback, checking, pendingCopy, headCheck, checkHead, cancelPending, resetCopy, writeClipboard, checkedComment,
    copyCheckedComment, copyAcknowledged, retryPending };
}
