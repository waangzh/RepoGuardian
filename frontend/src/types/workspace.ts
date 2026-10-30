export const reviewPrimarySections = [
  { id: 'overview', label: '概览', icon: 'dashboard' },
  { id: 'changes', label: '变更', icon: 'file-code' },
  { id: 'findings', label: '问题', icon: 'shield' },
  { id: 'activity', label: '活动', icon: 'activity' },
  { id: 'publish', label: '发布', icon: 'report' },
] as const;

export const reviewSecondarySections = [] as const;
export type ReviewSection = typeof reviewPrimarySections[number]['id'];
