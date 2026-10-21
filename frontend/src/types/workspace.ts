export type ReviewSection = "overview" | "findings" | "scope" | "files" | "activity" | "report" | "publish";

export interface ReviewNavItem {
  id: ReviewSection;
  label: string;
  icon: string;
}

/** 三个主要任务：先看结论、再看问题、最后交付报告。 */
export const reviewPrimarySections: ReviewNavItem[] = [
  { id: "overview", label: "结果概览", icon: "dashboard" },
  { id: "findings", label: "问题与证据", icon: "search" },
  { id: "report", label: "审查报告", icon: "report" },
];

/** 补充证据：按需查看，不与主要任务平级。 */
export const reviewSecondarySections: ReviewNavItem[] = [
  { id: "scope", label: "覆盖与限制", icon: "shield" },
  { id: "files", label: "变更文件", icon: "file" },
  { id: "activity", label: "技术记录", icon: "activity" },
];

/** 交付是操作而非信息页，从审查报告的 PR 评论草稿进入。 */
export const reviewPublishSection: ReviewNavItem = { id: "publish", label: "发布或复制到 GitHub", icon: "send" };

/** 全部页面用于标题查找；导航只渲染上面的主次分组。 */
export const reviewSections: ReviewNavItem[] = [
  ...reviewPrimarySections,
  ...reviewSecondarySections,
  reviewPublishSection,
];
