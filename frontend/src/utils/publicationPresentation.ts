/**
 * 发布流程的用户语言映射：协议值只用于判定，界面不直接展示内部术语。
 * 后端字段与状态枚举保持原样，翻译仅发生在展示层。
 */
import type { PublicationItem, PublicationState, PublicationInlinePreview } from "../types/publication";

export const publicationStateLabel: Record<PublicationState, string> = {
  queued: "已加入发布队列",
  preparing: "正在核对发布条件",
  publishing: "正在写入 GitHub",
  succeeded: "已发布到 GitHub",
  partial: "部分内容已发布",
  failed: "发布未完成",
  uncertain: "无法确认 GitHub 是否已收到评论",
  stale: "PR 已更新，请重新审查后发布",
};

export const publicationItemStateLabel: Record<PublicationItem["state"], string> = {
  pending: "等待写入",
  publishing: "正在写入",
  published: "已确认写入",
  failed: "写入失败",
  uncertain: "结果未知",
  summary_only: "无法定位到代码行，已写入总结评论",
};

/** 行级评论在预检中的处理方式；`create` 等协议值不直接出现在界面上。 */
export const inlineActionLabel: Record<PublicationInlinePreview["action"], string> = {
  create: "将发布为代码行评论",
  already_published: "相同评论已存在，本次不重复发布",
  summary_fallback: "无法准确定位到代码行，将写入总结评论",
  summary_only: "仅在总结评论中反馈，不创建代码行讨论",
};

/** 评论条目的中文称呼，替代 kind 的 summary/inline。 */
export function publicationItemKindLabel(kind: PublicationItem["kind"]) {
  return kind === "summary" ? "PR 总结评论" : "代码行评论";
}

const errorLabels: Record<string, string> = {
  publication_disabled: "服务端尚未启用 GitHub 发布；当前只能复制评论后手动粘贴。",
  publication_operator_unauthorized: "发布操作口令无效，或当前连接不符合本地单用户访问限制。",
  publication_stale: "PR 已更新，请重新审查后再发布。",
  publication_plan_changed: "审查内容或 PR 版本已变化，请重新预检后再发布。",
  publication_content_blocked: "本次审查结论未通过发布准入，暂时只能复制评论。",
  review_not_publishable: "审查尚未正常结束，暂时不能发布。",
  review_not_found: "审查记录不存在或已被清理。",
  github_permission_denied: "发布身份的 GitHub 权限不足，请核对 Token 的 Pull requests 写权限。",
  github_rate_limited: "GitHub 请求受限，请稍后重试。",
  github_not_found: "GitHub 未找到对应仓库、PR 或评论，请核对授权范围。",
  github_validation_failed: "GitHub 拒绝了写入内容，请检查评论正文或定位信息。",
  repository_not_authorized: "目标仓库不在发布授权范围内。",
  publisher_identity_mismatch: "发布 Token 对应的 GitHub 身份与服务端配置不一致。",
  repository_identity_mismatch: "PR 仓库与服务端记录的仓库不一致。",
  pr_not_open: "PR 已关闭或已合并，不能再写入评论。",
  invalid_finding_selection: "所选问题已失效，请重新预检。",
  too_many_inline_findings: "所选代码行评论数量超过服务端上限，请减少选择后重试。",
  publication_body_too_long: "评论正文超过 GitHub 长度限制，请减少选择的问题。",
  publication_plan_changed_conflict: "审查内容或 PR 版本已变化，请重新预检后再发布。",
  partial_acknowledgement_required: "请先明确确认部分审查或部分发布。",
  idempotency_key_conflict: "本次确认标识对应不同内容，请重新预检。",
  publication_busy: "该 PR 已有发布任务正在处理，请稍后刷新状态。",
  publication_not_found: "发布记录不存在。",
  publication_not_retryable: "当前状态不支持继续处理，请查看发布历史。",
  publication_internal_error: "发布过程中出现内部错误，结果保持未知，请检查远端状态。",
  summary_operation_uncertain: "总结评论存在未确认的写入，请从发布历史检查远端状态。",
  summary_newer_review: "已有更新版本的审查摘要，旧结果不能覆盖。",
  summary_modified_remotely: "总结评论已被其他人修改，系统不会覆盖。",
  summary_multiple_matches: "检测到多条受控总结评论，需要人工核对。",
  summary_identity_conflict: "总结评论身份校验失败，请人工核对。",
  remote_object_identity_conflict: "远端评论身份校验失败，请人工核对。",
  inline_identity_conflict: "已有代码行评论与本次内容不一致，需要人工核对。",
  inline_multiple_matches: "检测到多条相同的代码行评论，需要人工核对。",
  inline_result_unknown: "部分代码行评论结果未知，请检查远端状态。",
  batch_result_unknown: "批次写入结果未知，请检查远端状态。",
  batch_already_sent: "该批次已经发送，正在核对远端结果。",
  publication_lease_lost: "发布任务已被其他实例接管，请刷新状态。",
  publication_operator_unauthorized_local: "发布接口仅允许本机连接访问。",
  invalid_repository: "仓库标识无效。",
  invalid_publisher_request: "发布请求无效。",
  invalid_review_payload: "评论写入请求无效。",
  github_transport_error: "连接 GitHub 失败，请检查网络或代理后重试。",
  github_request_failed: "GitHub 请求失败，请稍后重试。",
  github_response_invalid: "GitHub 返回内容无法校验，请稍后重试。",
  github_pagination_incomplete: "GitHub 分页结果不完整，暂时无法安全核对。",
};

/** 未登记的错误码保留原文，便于排障，但不再拼成英文短句。 */
export function publicationErrorText(code: string | null | undefined) {
  if (!code) return "";
  return errorLabels[code] || `发布请求失败（${code}）`;
}

/** 预检告警来自 diff 映射原因，翻译为可执行的说明。 */
export function publicationWarningText(reason: string) {
  const labels: Record<string, string> = {
    diff_mapping_unavailable: "部分文件无法获取完整 diff，相关问题将写入总结评论。",
    diff_line_not_commentable: "问题所在行不在本次变更范围内，将写入总结评论。",
    diff_range_invalid: "问题行范围无效，将写入总结评论。",
  };
  return labels[reason] || `部分问题无法定位到代码行（${reason}），将写入总结评论。`;
}
