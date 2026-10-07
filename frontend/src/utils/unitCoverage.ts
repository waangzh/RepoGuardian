import type { ReviewUnitResult } from "../types/review";

export function unitCoverageComplete(result: ReviewUnitResult): boolean {
  if (result.status !== "completed" || (result.input_coverage && result.input_coverage.target_coverage !== "complete")) return false;
  const ledger = result.coverage_ledger;
  const manifest = result.diff_manifest;
  if (!ledger && !manifest) return true;
  if (!ledger || !manifest || ledger.manifest_hash !== manifest.input_hash || ledger.review_unit_id !== manifest.review_unit_id) return false;
  if (Object.keys(ledger.diff_hunks).length !== manifest.hunks.length || manifest.hunks.some((hunk) => ledger.diff_hunks[hunk.id]?.status !== "reviewed")) return false;
  return Object.values(ledger.targets).every((check) => !check.required || check.status === "checked")
    && Object.values(ledger.hypotheses).every((check) => !check.required || ["supported", "refuted"].includes(check.status))
    && Object.values(ledger.questions).every((question) => question.status !== "pending")
    && Object.values(ledger.dependencies).every((check) => check.status === "verified");
}
