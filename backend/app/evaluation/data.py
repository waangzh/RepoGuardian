"""冻结不含答案的输入清单；答案仅由评分阶段按哈希加载。"""

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PROTOCOL_VERSION = "aacr-review-v1"
SOURCE_REPOSITORY = "https://github.com/alibaba/aacr-bench"
PRODUCT_CATEGORIES = {"Code Defect", "Security Vulnerability", "Performance"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FrozenSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance_id: str = Field(min_length=1)
    repository: str
    pr_url: str
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    language: str
    changed_lines: int = Field(ge=0)
    # v1 不提供原始 PR 标题/正文，防止后续编辑和评审意见污染输入。
    metadata_policy: Literal["identity_only"] = "identity_only"

    @model_validator(mode="after")
    def validate_identity(self):
        match = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)", self.pr_url)
        if not match or self.repository != f"{match[1]}/{match[2]}":
            raise ValueError("PR URL 与 repository 不一致")
        if any(part in {".", ".."} for part in self.repository.split("/")):
            raise ValueError("非法 repository")
        return self


class FrozenFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    records: int = Field(ge=0)
    comments: int = Field(ge=0)


class DatasetLock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: Literal["aacr-review-v1"] = PROTOCOL_VERSION
    source_repository: Literal["https://github.com/alibaba/aacr-bench"] = SOURCE_REPOSITORY
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    files: dict[str, FrozenFile]
    samples: list[FrozenSample] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_manifest(self):
        if set(self.files) != {"positive_samples.json", "negative_samples.json"}:
            raise ValueError("必须冻结正负两个数据文件")
        if len({s.instance_id for s in self.samples}) != len(self.samples):
            raise ValueError("重复 instance_id")
        if self.files["positive_samples.json"].records != len(self.samples):
            raise ValueError("样本清单不完整")
        return self


def parse_records(data: bytes) -> list[dict]:
    records = json.loads(data)
    if not isinstance(records, list) or any(not isinstance(x, dict) for x in records):
        raise ValueError("AACR 原始数据必须为对象数组")
    return records


def sample_from_record(record: dict) -> FrozenSample:
    url = record["githubPrUrl"]
    repository = "/".join(url.split("/")[3:5])
    return FrozenSample(
        instance_id=f"{repository.replace('/', '__')}@{record['target_commit'][:7]}",
        repository=repository, pr_url=url, base_sha=record["source_commit"],
        head_sha=record["target_commit"], language=record["project_main_language"],
        changed_lines=record["change_line_count"],
    )


def freeze_dataset(positive: Path, negative: Path, revision: str) -> DatasetLock:
    from app.evaluation.isolation import require_reference_access
    require_reference_access()
    files = {}
    samples = []
    for name, path in (("positive_samples.json", positive), ("negative_samples.json", negative)):
        data = path.read_bytes()
        records = parse_records(data)
        files[name] = FrozenFile(sha256=sha256(data), records=len(records),
                                 comments=sum(len(x["comments"]) for x in records))
        if name == "positive_samples.json":
            samples = [sample_from_record(x) for x in records]
    return DatasetLock(source_revision=revision, files=files,
                       samples=sorted(samples, key=lambda x: x.instance_id))


def load_references(lock: DatasetLock, path: Path, *, negative: bool = False,
                    profile: Literal["official_all", "product_core"] = "official_all") -> dict:
    """仅供评分器调用：校验原始字节，保留 category/context/source 等所有字段。"""
    from app.evaluation.isolation import require_reference_access
    require_reference_access()
    name = "negative_samples.json" if negative else "positive_samples.json"
    data = path.read_bytes()
    if sha256(data) != lock.files[name].sha256:
        raise ValueError(f"{name} SHA256 不一致")
    records = parse_records(data)
    if not negative and sorted((sample_from_record(x).model_dump() for x in records),
                               key=lambda x: x["instance_id"]) != [s.model_dump() for s in lock.samples]:
        raise ValueError("输入清单与原始数据不一致")
    output = {}
    for record in records:
        sample = sample_from_record(record)
        comments = []
        for raw in record["comments"]:
            if profile == "product_core" and raw["category"] not in PRODUCT_CATEGORIES:
                continue
            start, end = raw["from_line"], raw["to_line"]
            if (isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int)
                    or not isinstance(end, int) or min(start, end) < 1
                    or raw["side"] not in {"left", "right"}):
                raise ValueError("非法参考位置")
            comments.append({**raw, "from_line": min(start, end), "to_line": max(start, end)})
        if sample.instance_id in output:
            raise ValueError("重复参考样本")
        output[sample.instance_id] = comments
    return output
