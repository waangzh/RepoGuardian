"""不调用模型的 diff 准备与标注坐标核验，生成独立 v2 输入锁。"""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.evaluation.data import DatasetLock, FrozenSample, load_references, sha256
from app.evidence import DiffIndex
from app.models.review import EvidenceCandidate
from app.tools.diff_parser import DiffParser
from app.tools.git_tool import DiffPreparationError, GitTool, GitToolError


def dataset_digest(lock: DatasetLock) -> str:
    # 父清单采用规范 JSON 指纹；原始正负数据仍校验字节哈希。
    return sha256(json.dumps(lock.model_dump(mode="json"), sort_keys=True,
                             ensure_ascii=False, separators=(",", ":")).encode())


class PreparedDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instance_id: str
    declared_base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    effective_base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    changed_lines: int = Field(ge=0)
    changed_files: list[str]
    alignment_status: Literal["ready", "quarantined"]


class DiffInputLock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: Literal["aacr-review-v2"] = "aacr-review-v2"
    diff_policy: Literal["merge_base"] = "merge_base"
    context_lines: Literal[80] = 80
    dataset_lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    samples: list[PreparedDiff] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({x.instance_id for x in self.samples}) != len(self.samples):
            raise ValueError("duplicate diff input ID")
        return self

    def validate_dataset(self, dataset: DatasetLock):
        if self.dataset_lock_sha256 != dataset_digest(dataset):
            raise ValueError("diff input parent lock mismatch")
        by_id = {s.instance_id: s for s in dataset.samples}
        for item in self.samples:
            source = by_id.get(item.instance_id)
            if not source or source.base_sha != item.declared_base_sha or source.head_sha != item.head_sha:
                raise ValueError("diff input SHA/identity mismatch")


def unique_merge_base(git: GitTool, repo: Path, base: str, head: str, *, cancel_event=None) -> str:
    bases = git.merge_bases(repo, base, head, cancel_event=cancel_event)
    if len(bases) != 1:
        raise GitToolError("snapshot_requires_unique_merge_base")
    return bases[0]


def inspect_diff(git: GitTool, repo: Path, sample: FrozenSample, references: list[dict]):
    for revision in (sample.base_sha, sample.head_sha):
        actual = git._run([git._git, "-C", str(repo), "rev-parse", revision + "^{commit}"]).strip()
        if actual != revision:
            raise GitToolError("snapshot_revision_mismatch")
    try:
        # 与生产、Preview 共用同一 Diff 生成函数，只把错误名映射回锁定协议。
        effective, diff = git.unified_diff(repo, sample.base_sha, sample.head_sha)
    except DiffPreparationError as exc:
        raise GitToolError("snapshot_requires_unique_merge_base") from exc
    changed = DiffParser().parse(diff)
    index = DiffIndex(changed)
    failures = []
    for number, ref in enumerate(references):
        side = {"left": "base", "right": "head"}[ref["side"]]
        candidate = EvidenceCandidate(file_path=ref["path"], side=side,
                                      start_line=ref["from_line"], end_line=ref["to_line"])
        path = index.canonical_path(ref["path"], side)
        reason = None
        if not path:
            reason = "reference_file_not_changed"
        elif not index.is_commentable(candidate):
            reason = "reference_outside_diff_hunks"
        else:
            revision = effective if side == "base" else sample.head_sha
            text = git.get_file_content_at_revision(repo, revision, path)
            if text is None or candidate.end_line > len(text.splitlines()):
                reason = "reference_outside_snapshot_file"
        if reason:
            failures.append({"reference_index": number, "path": ref["path"], "side": side,
                             "start_line": candidate.start_line, "end_line": candidate.end_line,
                             "reason": reason})
    numstat = git._run([git._git, "-C", str(repo), "diff", "--numstat", effective, sample.head_sha])
    changed_lines = sum(sum(int(x) for x in row.split("\t")[:2]) for row in numstat.splitlines()
                        if all(x.isdigit() for x in row.split("\t")[:2]))
    prepared = PreparedDiff(instance_id=sample.instance_id, declared_base_sha=sample.base_sha,
        head_sha=sample.head_sha, effective_base_sha=effective, diff_sha256=sha256(diff.encode()),
        changed_lines=changed_lines, changed_files=[x.file_path for x in changed],
        alignment_status="quarantined" if failures else "ready")
    audit = {"instance_id": sample.instance_id, "reference_count": len(references),
             "aligned_count": len(references) - len(failures), "failures": failures,
             "declared_changed_lines": sample.changed_lines, "actual_changed_lines": changed_lines}
    return prepared, audit


def prepare_diff_inputs(lock_path: Path, run_dirs: list[Path], references_path: Path):
    data = lock_path.read_bytes()
    lock = DatasetLock.model_validate_json(data)
    refs = load_references(lock, references_path)
    by_id = {s.instance_id: s for s in lock.samples}
    prepared, audits, seen = [], [], set()
    git = GitTool()
    for run_dir in run_dirs:
        summary = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        if summary["lock_sha256"] != sha256(data):
            raise ValueError("source run dataset hash mismatch")
        for item in summary["samples"]:
            if not item.get("result_file"):
                raise ValueError("source sample has no snapshot result")
            result_path = (run_dir / item["result_file"]).resolve()
            if not result_path.is_relative_to(run_dir.resolve()):
                raise ValueError("result path outside run directory")
            raw = json.loads(result_path.read_text(encoding="utf-8"))
            sample = FrozenSample.model_validate(raw["sample"])
            if sample.instance_id in seen or sample != by_id.get(sample.instance_id):
                raise ValueError("source snapshot sample mismatch/duplicate")
            seen.add(sample.instance_id)
            if not isinstance(raw.get("snapshot"), dict):
                raise ValueError("source sample has no prepared Git snapshot")
            repo = Path(raw["snapshot"]["repo_path"]).resolve()
            if not repo.is_relative_to((run_dir / "workspaces").resolve()):
                raise ValueError("source snapshot outside workspaces")
            fact, audit = inspect_diff(git, repo, sample, refs[sample.instance_id])
            prepared.append(fact)
            audits.append(audit)
    return DiffInputLock(dataset_lock_sha256=dataset_digest(lock), samples=prepared), {
        "protocol_version": "aacr-review-v2", "dataset_lock_sha256": dataset_digest(lock),
        "reference_file_sha256": lock.files["positive_samples.json"].sha256, "samples": audits,
    }
