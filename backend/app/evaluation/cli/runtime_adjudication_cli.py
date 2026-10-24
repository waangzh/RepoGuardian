"""评分者离线验收质量标注；默认非盲态，不执行目标仓库代码。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from app.evaluation.runtime_adjudication import adjudicate_annotations
from app.tools.git_tool import GitTool


def make_source_reader(source_roots: list[dict], workspace_root: Path, *, git_tool=None):
    """只读评分者登记的临时仓库和固定 SHA，拒绝 symlink/submodule blob。"""
    boundary = workspace_root.resolve(strict=True)
    repositories = {}
    for source in source_roots:
        repository = source["repository"]
        root = Path(source["source_root"]).resolve(strict=True)
        if repository in repositories or root == boundary or not root.is_relative_to(boundary):
            raise ValueError("source_root_identity_or_boundary_rejected")
        revisions = {source["head_sha"], source["base_sha"]}
        if any(not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha)
               for sha in revisions):
            raise ValueError("source_root_requires_immutable_revisions")
        repositories[repository] = (root, revisions)
    git = git_tool or GitTool(workdir=boundary)
    cache = {}

    def read(repository: str, sha: str, path: str) -> str | None:
        if repository not in repositories or sha not in repositories[repository][1]:
            return None
        root, _ = repositories[repository]
        key = (repository, sha, path)
        if key not in cache:
            entries = git.get_tree_entries(root, sha, [path])
            entry = entries.get(path)
            cache[key] = (git.get_file_content_at_revision(root, sha, path)
                          if entry and entry["mode"] in {"100644", "100755"} else None)
        return cache[key]

    return read


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packets", required=True, type=Path)
    parser.add_argument("--annotations", required=True, type=Path)
    parser.add_argument("--source-roots", required=True, type=Path)
    parser.add_argument("--workspace-root", required=True, type=Path)
    parser.add_argument("--reviewer-id", required=True)
    parser.add_argument("--blind", action="store_true",
                        help="评分者确认评审者未看 pipeline 或参考答案时才使用")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    reader = make_source_reader(json.loads(args.source_roots.read_text(encoding="utf-8")),
                                args.workspace_root)
    report = adjudicate_annotations(
        json.loads(args.packets.read_text(encoding="utf-8")),
        json.loads(args.annotations.read_text(encoding="utf-8")),
        source_reader=reader, reviewer_id=args.reviewer_id, blind=args.blind,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"adjudication_status": report["adjudication_status"],
                      "quality": report["quality"], "rejected": len(report["rejected"])},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
