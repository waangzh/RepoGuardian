"""离线核验真实接入产物；在审查完成后读取参考答案，不调用模型。"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.evaluation.data import DatasetLock, load_references, sha256  # noqa: E402
from app.evaluation.runner import write_json  # noqa: E402
from app.evidence import DiffIndex  # noqa: E402
from app.tools.diff_parser import DiffParser  # noqa: E402
from app.tools.git_tool import DIFF_CONTEXT_LINES, GitTool  # noqa: E402
from app.review.tool_scope import validate_repository_file  # noqa: E402
from app.models.review import EvidenceCandidate  # noqa: E402


def normalize(text):
    return " ".join(text.split())


def check_result(result, run_dir, references):
    snapshot = result.get("snapshot")
    findings = []
    verification = {"snapshot": "unverified", "evidence": [], "reference_input_matches": [],
                    "tool_paths": [], "tool_path_failures": [], "scope_failures": []}
    if snapshot:
        repo = Path(snapshot["repo_path"]).resolve()
        if not repo.is_relative_to((run_dir / "workspaces").resolve()):
            raise ValueError("snapshot outside isolated workspaces")
        git = GitTool()
        sample = result["sample"]
        head = git._run(["git", "-C", str(repo), "rev-parse", "HEAD"]).strip()
        declared = git._run(["git", "-C", str(repo), "rev-parse", sample["base_sha"] + "^{commit}"]).strip()
        base = snapshot["verified_base"]
        valid_base = base == declared
        if snapshot.get("diff_policy") == "merge_base":
            from app.evaluation.diff_inputs import unique_merge_base

            # 内容基准必须是 merge base；带 v2 输入锁时再核对锁定身份与哈希。
            valid_base = base == unique_merge_base(git, repo, declared, head)
            if result.get("diff_input"):
                valid_base = (valid_base and
                              base == result["diff_input"].get("effective_base_sha") and
                              declared == result["diff_input"].get("declared_base_sha") and
                              head == result["diff_input"].get("head_sha") and
                              snapshot["diff_sha256"] == result["diff_input"].get("diff_sha256"))
        diff = git._run(["git", "-C", str(repo), "diff", "--no-ext-diff",
                         f"--unified={DIFF_CONTEXT_LINES}", base, head])
        clean = not git._run(["git", "-C", str(repo), "status", "--porcelain"]).strip()
        verification["snapshot"] = "passed" if (head == sample["head_sha"] and declared == sample["base_sha"]
            and valid_base
            and sha256(diff.encode()) == snapshot["diff_sha256"] and clean) else "failed"
        verification["actual_head"] = head
        verification["actual_base"] = base
        verification["declared_base"] = declared
        verification["diff_policy"] = snapshot.get("diff_policy", "declared_base")
        verification["worktree_clean"] = clean
        changed = DiffParser().parse(diff)
        index = DiffIndex([x.model_dump(mode="json") for x in changed])
        verification["changed_files"] = [x.file_path for x in changed]
        for issue in [*result["confirmed"], *result["needs_human"]]:
            anchor = issue["evidence"]
            side = anchor.get("resolved_side")
            start, end = anchor.get("resolved_start_line"), anchor.get("resolved_end_line")
            path = index.canonical_path(anchor["file_path"], side) if side in {"head", "base"} else None
            passed = False
            if path and start and end:
                try:
                    text = (git.get_file_content_at_revision(repo, base, path) if side == "base" else
                            validate_repository_file(repo, path).read_text(encoding="utf-8", errors="replace"))
                    lines = (text or "").splitlines()
                    placed = "\n".join(lines[start-1:end])
                    passed = (1 <= start <= end <= len(lines) and
                              normalize(placed) == normalize(anchor["existing_code"]) and
                              anchor.get("match_count") == 1 and
                              issue["side"] == {"head": "right", "base": "left"}.get(side))
                except (OSError, ValueError):
                    pass
            verification["evidence"].append({"issue_id": issue["issue_id"], "path": path,
                "side": side, "start_line": start, "end_line": end, "passed": passed,
                "commentable": index.is_commentable(EvidenceCandidate(
                    file_path=path, side=side, start_line=start, end_line=end))
                    if path and start and end else False})
        for event in result.get("tool_reads", []):
            if event["status"] != "completed":
                continue
            scope = event["scope"]
            for item in event.get("results", []):
                path = item if isinstance(item, str) else item.get("file")
                if not path:
                    continue
                verification["tool_paths"].append(path)
                try:
                    validate_repository_file(repo, path)
                except (OSError, ValueError):
                    verification["tool_path_failures"].append(path)
                if (event["tool"] not in {"file_find", "code_search"} and
                        path not in scope["readable_files"]):
                    verification["scope_failures"].append(path)
    wire = [x for x in result.get("model_requests", []) if x["status"] == "started"]
    for event in wire:
        content = normalize(event["system"] + "\n" + event["prompt"])
        for ref in references:
            note = normalize(ref["note"])
            if len(note) >= 120 and note in content:
                verification["reference_input_matches"].append({"request_id": event["request_id"],
                    "attempt": event["attempt"], "reference_path": ref["path"]})
    verification["wire_attempts"] = len(wire)
    outcomes = [x for x in result.get("model_requests", []) if x["status"] != "started"]
    verification["wire_outcomes"] = dict(Counter(x["status"] for x in outcomes))
    verification["retries"] = sum(x["attempt"] > 1 for x in wire)
    verification["wire_observed_tokens"] = sum((x.get("usage") or {}).get("actual_total_tokens") or 0
                                              for x in outcomes)
    missing = sum(x["status"] != "completed" or (x.get("usage") or {}).get("actual_total_tokens") is None
                  for x in outcomes)
    verification["wire_unknown_usage_attempts"] = missing
    costs = [(x.get("usage") or {}).get("cost_microusd") for x in outcomes]
    verification["cost_microusd"] = (sum(costs) if outcomes and not missing and
                                    all(x is not None for x in costs) else None)
    stages = Counter(x["stage"] for x in result["issue_audit"])
    verification["audit_stages"] = dict(stages)
    verification["final_dispositions"] = dict(Counter(x["status"] for x in result["issue_audit"]
                                                     if x["stage"] == "final"))
    if verification["snapshot"] != "passed":
        findings.append("历史快照未核验通过")
    if any(not x["passed"] for x in verification["evidence"]):
        findings.append("存在需复核的位置/证据")
    if verification["reference_input_matches"]:
        findings.append("模型输入存在完整参考意见文本匹配，需检查来源")
    if verification["tool_path_failures"] or verification["scope_failures"]:
        findings.append("存在需复核的工具读取路径")
    if result["status"] not in {"completed", "completed_with_warnings"}:
        findings.append("审查未完整完成：" + result["status"])
    if missing:
        findings.append(f"{missing} 次请求失败/取消或缺少实测用量，不能计算完整成本")
    verification["findings"] = findings
    return verification


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--lock", type=Path, default=Path("evaluation/aacr.lock.json"))
    parser.add_argument("--references", type=Path, default=Path(".repoguardian/evaluation-data/positive_samples.json"))
    args = parser.parse_args()
    lock = DatasetLock.model_validate_json(args.lock.read_bytes())
    refs = load_references(lock, args.references)
    summary = json.loads((args.run / "run.json").read_text(encoding="utf-8"))
    checks = []
    for sample in summary["samples"]:
        if not sample.get("result_file"):
            checks.append({"instance_id": sample["instance_id"], "status": sample["status"],
                           "findings": ["没有终态结果"]})
            continue
        result = json.loads((args.run / sample["result_file"]).read_text(encoding="utf-8"))
        checks.append({"instance_id": sample["instance_id"], "status": result["status"],
            "duration_ms": result["duration_ms"], "confirmed": len(result["confirmed"]),
            "needs_human": len(result["needs_human"]), "coverage": result["coverage"],
            **check_result(result, args.run, refs[sample["instance_id"]])})
    write_json(args.run / "acceptance.json", {"checks": checks})
    for check in checks:
        print(json.dumps({k:v for k,v in check.items() if k not in
            {"coverage", "evidence", "tool_paths"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
