"""显式授权、GitHub 身份、版本与精确 diff 的只读预检。"""
from datetime import timezone
import asyncio
import re

from app.core.fingerprints import stable_hash
from app.services.publication.publication_planner import PublicationPlanner
from app.tools.github.github_tool import parse_pr_url
from app.tools.github.github_publisher import GitHubPublishError
from app.tools.github.github_diff_mapper import GitHubDiffMapper, MappingFailure


class PublicationPreflight:
    def __init__(self, publisher, config, artifacts=None):
        self.github, self.config = publisher, config
        self.artifacts = artifacts

    async def check(self, task, finding_keys=None):
        config = self.config
        if not config.repoguardian_github_publish_enabled:
            raise GitHubPublishError("publication_disabled")
        if task.status not in {"completed", "partial"}:
            raise GitHubPublishError("review_not_publishable")
        plan = await asyncio.to_thread(PublicationPlanner(self.artifacts).build, task)
        if not plan.content_eligible:
            raise GitHubPublishError("publication_content_blocked")
        owner, repo, number = parse_pr_url(task.pr_url)
        path = self.github.repo_path(owner, repo)
        user = await self.github.get("/user")
        if type(user.get("id")) is not int or user["id"] != config.repoguardian_github_publish_publisher_id:
            raise GitHubPublishError("publisher_identity_mismatch")
        repository = await self.github.get(path)
        repository_id = repository.get("id")
        if repository_id != task.snapshot.repository_id:
            raise GitHubPublishError("repository_identity_mismatch")
        allowlist = {int(value.strip()) for value in config.repoguardian_github_publish_allowed_repositories.split(",") if value.strip().isdigit()}
        if type(repository_id) is not int or repository_id not in allowlist or repository.get("full_name", "").casefold() != f"{owner}/{repo}".casefold():
            raise GitHubPublishError("repository_not_authorized")
        permissions = repository.get("permissions") or {}
        if (not config.repoguardian_github_publish_pull_requests_write_confirmed
                or not any(permissions.get(role) is True for role in ("push", "maintain", "admin"))):
            raise GitHubPublishError("github_permission_denied")
        pr = await self.github.get(f"{path}/pulls/{number}")
        snapshot = task.snapshot
        if f"{task.pr.owner}/{task.pr.repo}".casefold() != f"{owner}/{repo}".casefold():
            raise GitHubPublishError("repository_identity_mismatch")
        if (pr.get("number") != number or (pr.get("base", {}).get("repo") or {}).get("id") != repository_id
                or task.pr.number != number):
            raise GitHubPublishError("repository_identity_mismatch")
        if pr.get("state") != "open" or pr.get("merged") is not False:
            raise GitHubPublishError("pr_not_open")
        if (not re.fullmatch(r"[a-f0-9]{40,64}", snapshot.declared_base_sha)
                or pr.get("head", {}).get("sha") != snapshot.head_sha
                or pr.get("base", {}).get("sha") != snapshot.declared_base_sha):
            raise GitHubPublishError("publication_stale")
        findings = [*plan.inline_drafts, *plan.summary_findings]
        available = {item.finding_key for item in findings}
        keys = sorted(available if finding_keys is None else set(finding_keys))
        if not set(keys) <= available:
            raise GitHubPublishError("invalid_finding_selection")
        inline = [item for item in plan.inline_drafts if item.finding_key in keys]
        if len(inline) > config.repoguardian_github_publish_max_inline:
            raise GitHubPublishError("too_many_inline_findings")
        files = await self.github.paginate(f"{path}/pulls/{number}/files") if inline else []
        complete = not inline or (type(pr.get("changed_files")) is int and len(files) == pr["changed_files"] and len(files) <= 3000)
        locations, fallback = {}, {}
        for finding in inline:
            mapped = GitHubDiffMapper().map_finding(finding, files) if complete else MappingFailure("diff_mapping_unavailable")
            if isinstance(mapped, MappingFailure):
                fallback[finding.finding_key] = mapped.reason
            else:
                locations[finding.finding_key] = mapped
        context = {"owner": owner, "repo": repo, "pr_number": number, "repository_id": repository_id,
                   "publisher_identity": f"user:{user['id']}", "publisher_id": user["id"], "base_sha": snapshot.declared_base_sha,
                   "order": (task.completed_at or task.updated_at).astimezone(timezone.utc).isoformat() + ":" + task.id,
                   "report_version": plan.report_version}
        partial = plan.outcome.kind == "inconclusive" or plan.outcome.coverage != "complete" or bool(fallback) or bool(plan.withheld_findings) or len(keys) != len(available)
        return {"ready": True, "review_id": task.id, "report_version": plan.report_version,
                "plan_hash": stable_hash(plan.model_dump(mode="json")),
            "head_sha": plan.snapshot.head_sha, "head_sha_matches": True, "pr_open": True, "permissions_ok": True,
            "summary_action": "inspect", "summary_comment_id": None, "finding_keys": keys,
            "inline": {"selected": len(inline), "ready": len(locations), "already_published": 0,
                       "summary_fallback": len(fallback), "blocked": 0}, "requires_partial_acknowledgement": partial,
            "warnings": sorted(set(fallback.values())), "locations": locations, "fallback": fallback,
            "context": context, "plan": plan}

    async def validate_current(self, context, head_sha):
        path = self.github.repo_path(context["owner"], context["repo"])
        pr = await self.github.get(f"{path}/pulls/{context['pr_number']}")
        if pr.get("head", {}).get("sha") != head_sha or pr.get("base", {}).get("sha") != context["base_sha"]:
            raise GitHubPublishError("publication_stale")
        if (pr.get("state") != "open" or pr.get("merged") is not False
                or (pr.get("base", {}).get("repo") or {}).get("id") != context["repository_id"]):
            raise GitHubPublishError("pr_not_open")
