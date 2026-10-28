export const reviewPrimarySections = [
  { id: 'overview', label: '结果概览', icon: 'dashboard' },
  { id: 'findings', label: '问题与证据', icon: 'shield' },
  { id: 'scope', label: '覆盖与限制', icon: 'units' },
  { id: 'report', label: '审查意见', icon: 'report' },
] as const;
export const reviewSecondarySections = [
  { id: 'files', label: '变更文件', icon: 'file-code' },
  { id: 'activity', label: 'Agent 活动', icon: 'activity' },
  { id: 'discussions', label: 'PR 讨论', icon: 'context' },
] as const;
export type ReviewSection = typeof reviewPrimarySections[number]['id']
  | typeof reviewSecondarySections[number]['id'];
