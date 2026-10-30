"""Conservative reuse: unchanged bytes are history, current defects are rechecked."""

from app.review.planner import identity, members, tokens
from app.tools.git_tool import GitToolError

DOCUMENTS = (".md", ".mdx", ".rst", ".adoc")


def refine_plan(run, previous, previous_work, git, repo, history):
    included = {path for unit in run.plan.units for path in unit.paths}
    run.plan.history = [item for item in history if any(
        source["path"] in included for source in item["sources"])]
    if previous is None or previous.report is None:
        run.plan.incremental_reason = "no_prior_report"
        return
    if previous.snapshot.head_sha == run.snapshot.head_sha:
        run.plan.incremental_reason = "explicit_same_snapshot_review"
        return
    if (previous.snapshot.base_sha != run.snapshot.base_sha
            or previous.snapshot.declared_base_sha != run.snapshot.declared_base_sha
            or previous.configuration != run.configuration):
        run.plan.incremental_reason = "baseline_or_configuration_changed"
        return
    try:
        changed_paths = git.changed_paths_between(repo, previous.snapshot.head_sha, run.snapshot.head_sha)
    except (GitToolError, ValueError, OSError):
        run.plan.incremental_reason = "head_history_not_comparable"
        return
    # There is no compiler dependency closure. Source/config/test changes therefore
    # get a full pass; document-only updates can reuse identical old source units.
    if any(not path.casefold().endswith(DOCUMENTS) for path in changed_paths):
        run.plan.incremental_reason = "source_change_requires_full_review"
        changed_units = {unit.id for unit in run.plan.units if set(unit.paths) & changed_paths}
        run.plan.units.sort(key=lambda unit: (unit.id not in changed_units,
            {"high": 0, "medium": 1, "low": 2}[unit.risk], unit.paths))
        return
    old_members, new_members = members(previous.changed_files), members(run.changed_files)
    completed = {key: work
        for unit in previous.plan.units
        for work in previous_work if work.unit_id == unit.id and work.kind == "review"
        and work.status == "completed" and work.material_delivery == "complete"
        and not (work.result or {}).get("unresolved_reason")
        for key in unit.required_hunk_ids}
    units, reused = [], False
    for unit in run.plan.units:
        groups, fresh = {}, []
        for key in unit.required_hunk_ids:
            member = new_members[key]
            match = completed.get(key)
            paths = sorted({member["path"], member["old_path"]})
            if not match or set(paths) & changed_paths or old_members.get(key) != member:
                fresh.append(key)
                continue
            try:
                old = git.get_tree_entries(repo, previous.snapshot.head_sha, paths)
                new = git.get_tree_entries(repo, run.snapshot.head_sha, paths)
            except (ValueError, OSError):
                fresh.append(key)
                continue
            if old != new:
                fresh.append(key)
                continue
            groups.setdefault(match.id, []).append(key)
        for previous_work_id, keys in [(None, fresh), *groups.items()]:
            if not keys:
                continue
            changed = unit.model_copy(update={
                "id": identity([run.snapshot.model_dump(), keys, unit.focus, run.plan.version])[:24],
                "paths": sorted({new_members[key]["path"] for key in keys}),
                "required_hunk_ids": keys,
                "estimated_input_tokens": sum(tokens(new_members[key]) for key in keys),
                "reused_from": f"{previous.id}:{previous_work_id}" if previous_work_id else None})
            units.append(changed)
            reused |= previous_work_id is not None
    run.plan.units = units
    run.plan.incremental_from = previous.id if reused else None
    run.plan.incremental_reason = "document_only_identical_blobs" if reused else "no_reusable_unit"
