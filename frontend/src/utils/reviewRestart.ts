import type { ReviewTask } from "../types/review";
import type { ReviewPlanOptions } from "../types/reviewPlan";
import { validPullRequestUrl } from "./workspacePresentation";

/** Only copy editable choices; snapshot identities and execution permissions belong to the new run. */
export function rereviewInput(task: ReviewTask) {
  const prUrl = task.pr_url.trim();
  if (!validPullRequestUrl(prUrl)) return null;
  const saved = task.confirmed_plan;
  const plan: ReviewPlanOptions | undefined = saved?.version === "review-plan-v1" ? {
    version: saved.version,
    depth: saved.depth,
    focus: [...saved.focus],
    scope: { mode: "all", primary_files: [] },
    limits: { ...saved.limits },
  } : undefined;
  const model = task.model?.trim() || "";
  return { prUrl, model, plan };
}
