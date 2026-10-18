export const groupingReasonLabel: Record<string, string> = {
  deletion_group: "删除文件组",
  migration_with_model: "数据库迁移与模型",
  api_with_model: "接口与模型",
  migration_file: "数据库迁移",
  dependency_file: "依赖清单",
  workflow_file: "CI 工作流",
  configuration_file: "配置文件",
  localized_resources: "本地化资源",
  implementation_with_tests: "实现与测试",
  large_file_symbol_split: "大文件按符号拆分",
  large_file_hunk_split: "大文件按 Hunk 拆分",
  single_file: "单文件",
  single: "单文件",
  cross_unit_followup: "跨组跟进",
  "task-policy-view": "任务策略视图",
};

const SYMBOL_TITLE_MAX = 42;

export function groupingReasonText(reason: string | undefined | null): string {
  if (!reason) return "";
  return groupingReasonLabel[reason] || reason;
}

/** 保留末两级目录，避免同名文件无法区分。 */
export function shortPath(path: string): string {
  const parts = (path || "").split(/[\\/]/).filter(Boolean);
  if (parts.length <= 2) return path || "";
  return parts.slice(-2).join("/");
}

function basename(path: string): string {
  return path.split(/[\\/]/).pop() || path;
}

export function readableSymbol(symbol: string): string {
  return symbol.replace(/[-_]+/g, " ").trim();
}

/**
 * 变更组标题：以文件为主定位锚点，符号降为辅助信息。
 * 保证同一文件拆分的多个单元可通过序号区分。
 */
export function unitTitle(unit: { changed_symbols: string[]; primary_files: string[]; id: string }, index?: number, total?: number): string {
  const label = shortPath(unit.primary_files[0] || "") || basename(unit.id);
  return typeof index === "number" && total && total > 1 ? `${label} · ${index + 1}/${total}` : label;
}

/** 变更符号摘要：单个时展示可读化符号，多个时只报数量，不猜优先级。 */
export function unitSymbolSummary(unit: { changed_symbols: string[] }): string {
  const symbols = unit.changed_symbols.filter(Boolean);
  if (!symbols.length) return "";
  if (symbols.length === 1) {
    const text = readableSymbol(symbols[0]);
    return text.length > SYMBOL_TITLE_MAX ? `${text.slice(0, SYMBOL_TITLE_MAX - 3).trimEnd()}…` : text;
  }
  return `${symbols.length} 个变更符号`;
}
