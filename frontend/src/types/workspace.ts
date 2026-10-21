export type ReviewSection = "overview" | "findings" | "scope" | "files" | "activity" | "report" | "publish";
export const reviewSections: Array<{ id: ReviewSection; label: string; icon: string }> = [
  { id: "overview", label: "结果概览", icon: "dashboard" },
  { id: "findings", label: "问题与证据", icon: "search" },
  { id: "scope", label: "覆盖与限制", icon: "shield" },
  { id: "files", label: "变更文件", icon: "file" },
  { id: "activity", label: "技术记录", icon: "activity" },
  { id: "report", label: "审查报告", icon: "report" },
  { id: "publish", label: "交付到 GitHub", icon: "send" },
];
