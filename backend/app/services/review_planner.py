"""确定性 Review Unit Planner：文件选择、分类、分组、预算与指纹。"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable

from app.core.config import settings
from app.models.review import (
    ChangedFile,
    ExcludedReviewFile,
    PlannedChangedFile,
    ReviewPlan,
    ReviewToolScope,
    ReviewUnit,
    ReviewUnitComplexity,
)

PLANNER_VERSION = "review-unit-planner-v1"

_BINARY_EXTENSIONS = {
    ".7z", ".a", ".avi", ".bin", ".bmp", ".class", ".dll", ".dylib",
    ".exe", ".gif", ".gz", ".ico", ".jar", ".jpeg", ".jpg", ".mov",
    ".mp3", ".mp4", ".o", ".pdf", ".png", ".pyc", ".so", ".tar",
    ".ttf", ".wav", ".webm", ".woff", ".woff2", ".xz", ".zip",
}
_GENERATED_NAMES = {
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock",
    "pdm.lock", "uv.lock", "cargo.lock", "go.sum", "composer.lock",
}
_DEPENDENCY_MANIFESTS = {
    "package.json", "pyproject.toml", "requirements.txt", "setup.py", "setup.cfg",
    "pipfile", "cargo.toml", "go.mod", "pom.xml", "build.gradle",
    "build.gradle.kts", "composer.json", "gemfile",
}
_CONFIG_NAMES = {
    ".editorconfig", ".pre-commit-config.yaml", "dockerfile", "makefile",
    "tox.ini", "pytest.ini", "tsconfig.json", "vite.config.ts", "vite.config.js",
}
_RESOURCE_EXTENSIONS = {".json", ".yaml", ".yml", ".toml", ".po", ".properties"}
_HIGH_RISK_TAGS = {"auth", "payment", "migration", "concurrency", "dependency", "workflow"}
_PUBLIC_SYMBOL = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:def|class|function|interface|type|func)\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
)
_ASSIGNMENT_SYMBOL = re.compile(
    r"^\s*export\s+(?:default\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)"
)


@dataclass(frozen=True)
class _UnitDraft:
    primary_files: tuple[str, ...]
    grouping_reason: str
    hunk_ids: tuple[str, ...] | None = None
    symbol_filter: tuple[str, ...] | None = None


class DeterministicReviewPlanner:
    """不调用模型、命令执行器或目标代码的可复现 Planner。"""

    def __init__(
        self,
        *,
        small_max_changed_lines: int | None = None,
        large_min_changed_lines: int | None = None,
        max_lines_per_read: int | None = None,
        max_search_results: int | None = None,
    ) -> None:
        self.small_max_changed_lines = (
            small_max_changed_lines
            if small_max_changed_lines is not None
            else settings.repoguardian_review_unit_small_max_changed_lines
        )
        self.large_min_changed_lines = (
            large_min_changed_lines
            if large_min_changed_lines is not None
            else settings.repoguardian_review_unit_large_min_changed_lines
        )
        self.max_lines_per_read = (
            max_lines_per_read
            if max_lines_per_read is not None
            else settings.repoguardian_review_unit_max_lines_per_read
        )
        self.max_search_results = (
            max_search_results
            if max_search_results is not None
            else settings.repoguardian_review_unit_max_search_results
        )

    def plan(
        self,
        changed_files: list[ChangedFile] | list[dict[str, Any]],
        *,
        base_sha: str,
        head_sha: str,
        file_index: list[dict[str, Any]] | None = None,
        symbol_index: list[dict[str, Any]] | None = None,
    ) -> ReviewPlan:
        files = [
            item if isinstance(item, ChangedFile) else ChangedFile.model_validate(item)
            for item in changed_files
        ]
        files.sort(key=lambda item: item.file_path)
        classified = {item.file_path: self._classify(item) for item in files}
        dispositions: list[PlannedChangedFile] = []
        excluded: list[ExcludedReviewFile] = []
        included: list[ChangedFile] = []

        for item in files:
            tags = classified[item.file_path]
            reason = self._excluded_reason(item.file_path, tags)
            dispositions.append(PlannedChangedFile(
                file_path=item.file_path,
                old_file_path=item.old_file_path,
                change_type=item.change_type,
                additions=item.additions,
                deletions=item.deletions,
                classifications=tags,
                included=reason is None,
                excluded_reason=reason,
            ))
            if reason is None:
                included.append(item)
            else:
                excluded.append(ExcludedReviewFile(
                    file_path=item.file_path,
                    reason=reason,
                    classifications=tags,
                ))

        drafts = self._group_files(included, classified)
        by_path = {item.file_path: item for item in included}
        hunk_ids = {
            item.file_path: [self.hunk_id(item.file_path, index, hunk.model_dump(mode="json"))
                             for index, hunk in enumerate(item.hunks)]
            for item in included
        }
        units = [
            self._build_unit(
                draft,
                by_path,
                classified,
                hunk_ids,
                base_sha,
                head_sha,
                file_index or [],
                symbol_index or [],
            )
            for draft in drafts
        ]
        units.sort(key=lambda unit: (unit.primary_files, unit.diff_hunk_ids, unit.id))
        self._validate_primary_ownership(units)
        warnings = []
        if excluded:
            warnings.append(f"{len(excluded)} 个文件因二进制或生成文件规则被排除")
        return ReviewPlan(
            planner_version=PLANNER_VERSION,
            changed_files=dispositions,
            review_units=units,
            excluded_files=excluded,
            matched_rules=sorted({rule for unit in units for rule in unit.rule_ids}),
            risk_tags=sorted({tag for unit in units for tag in unit.risk_tags}),
            warnings=warnings,
        )

    def build_scope(self, unit: ReviewUnit) -> ReviewToolScope:
        return ReviewToolScope(
            review_unit_id=unit.id,
            commentable_files=set(unit.primary_files),
            readable_files=set(unit.primary_files) | set(unit.related_files),
            max_lines_per_read=self.max_lines_per_read,
            max_search_results=self.max_search_results,
        )

    def should_skip_plan(self, unit: ReviewUnit, changed_files: Iterable[ChangedFile]) -> bool:
        changed_by_path = {item.file_path: item for item in changed_files}
        changed_lines = sum(
            changed_by_path[path].additions + changed_by_path[path].deletions
            for path in unit.primary_files if path in changed_by_path
        )
        return (
            len(unit.primary_files) == 1
            and changed_lines <= self.small_max_changed_lines
            and not (_HIGH_RISK_TAGS & set(unit.risk_tags))
            and "public_api" not in unit.risk_tags
            and "cross_module" not in unit.risk_tags
        )

    @staticmethod
    def estimated_model_calls(unit: ReviewUnit) -> int:
        return 1 if unit.complexity == ReviewUnitComplexity.small else 2

    @staticmethod
    def hunk_id(file_path: str, index: int, hunk: dict[str, Any]) -> str:
        payload = json.dumps(
            {"file": file_path, "index": index, "hunk": hunk},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"hunk-{hashlib.sha256(payload.encode()).hexdigest()[:16]}"

    @classmethod
    def normalized_unit_diff(
        cls,
        unit: ReviewUnit | _UnitDraft,
        changed_files: dict[str, ChangedFile],
        all_hunk_ids: dict[str, list[str]],
    ) -> str:
        selected = set(unit.diff_hunk_ids if isinstance(unit, ReviewUnit) else (unit.hunk_ids or ()))
        payload: list[dict[str, Any]] = []
        for path in unit.primary_files:
            item = changed_files[path]
            hunks = []
            for index, hunk in enumerate(item.hunks):
                hid = all_hunk_ids[path][index]
                if not selected or hid in selected:
                    hunks.append({"id": hid, **hunk.model_dump(mode="json")})
            payload.append({
                "file_path": path,
                "old_file_path": item.old_file_path,
                "change_type": item.change_type,
                "hunks": hunks,
            })
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _group_files(
        self,
        files: list[ChangedFile],
        classified: dict[str, list[str]],
    ) -> list[_UnitDraft]:
        drafts: list[_UnitDraft] = []
        remaining = {item.file_path: item for item in files}

        deletions = sorted(path for path, item in remaining.items() if item.change_type == "deleted")
        if deletions:
            drafts.append(_UnitDraft(tuple(deletions), "deletion_group"))
            for path in deletions:
                remaining.pop(path)

        for special, reason in (
            ("migration", "migration_file"),
            ("dependency", "dependency_file"),
            ("workflow", "workflow_file"),
            ("config", "configuration_file"),
        ):
            paths = sorted(path for path in remaining if special in classified[path])
            for path in paths:
                drafts.append(_UnitDraft((path,), reason))
                remaining.pop(path)

        resource_groups: dict[str, list[str]] = defaultdict(list)
        for path in remaining:
            key = self._resource_key(path)
            if key:
                resource_groups[key].append(path)
        for paths in sorted(resource_groups.values()):
            if len(paths) < 2:
                continue
            paths.sort()
            drafts.append(_UnitDraft(tuple(paths), "localized_resources"))
            for path in paths:
                remaining.pop(path)

        tests = {path for path in remaining if "test" in classified[path]}
        sources = sorted(path for path in remaining if path not in tests)
        for source in sources:
            if source not in remaining:
                continue
            matches = sorted(test for test in tests if test in remaining and self._is_test_for(test, source))
            if matches:
                drafts.append(_UnitDraft((source, *matches), "implementation_with_tests"))
                remaining.pop(source)
                for path in matches:
                    remaining.pop(path)

        for path, item in sorted(remaining.items()):
            changed_lines = item.additions + item.deletions
            ids = tuple(
                self.hunk_id(path, index, hunk.model_dump(mode="json"))
                for index, hunk in enumerate(item.hunks)
            )
            if changed_lines >= self.large_min_changed_lines and len(ids) > 1:
                drafts.extend(_UnitDraft((path,), "large_file_hunk_split", (hid,)) for hid in ids)
            else:
                drafts.append(_UnitDraft((path,), "single_file"))
        return drafts

    def _build_unit(
        self,
        draft: _UnitDraft,
        changed_by_path: dict[str, ChangedFile],
        classified: dict[str, list[str]],
        all_hunk_ids: dict[str, list[str]],
        base_sha: str,
        head_sha: str,
        file_index: list[dict[str, Any]],
        symbol_index: list[dict[str, Any]],
    ) -> ReviewUnit:
        primary = list(draft.primary_files)
        selected_hunks = list(draft.hunk_ids or (
            hid for path in primary for hid in all_hunk_ids.get(path, [])
        ))
        symbols = self._changed_symbols(primary, changed_by_path, symbol_index)
        rules = sorted({rule for path in primary for rule in self._rules(classified[path])})
        risks = sorted({risk for path in primary for risk in self._risks(path, classified[path])})
        if symbols and any("test" not in classified[path] for path in primary):
            risks.append("public_api")
        if len({PurePosixPath(path).parent for path in primary}) > 1:
            risks.append("cross_module")
        risks = sorted(set(risks))
        related = self._related_files(primary, file_index)
        normalized = self.normalized_unit_diff(draft, changed_by_path, all_hunk_ids)
        changed_lines = sum(
            changed_by_path[path].additions + changed_by_path[path].deletions for path in primary
        )
        complexity = self._complexity(changed_lines, primary, risks)
        estimated_tokens = max(
            512,
            (len(normalized) + 3) // 4 + 180 * len(primary + related) + 80 * len(symbols + rules),
        )
        identity = {
            "base_sha": base_sha,
            "head_sha": head_sha,
            "primary_files": primary,
            "hunk_ids": selected_hunks,
            "grouping_reason": draft.grouping_reason,
            "planner_version": PLANNER_VERSION,
        }
        unit_id = "ru-" + self._digest(identity)[:16]
        fingerprint = self._digest({
            "base_sha": base_sha,
            "head_sha": head_sha,
            "normalized_unit_diff": normalized,
            "primary_files": primary,
            "related_files": related,
            "rule_ids": rules,
            "planner_version": PLANNER_VERSION,
        })
        return ReviewUnit(
            id=unit_id,
            primary_files=primary,
            related_files=related,
            diff_hunk_ids=selected_hunks,
            changed_symbols=symbols,
            rule_ids=rules,
            risk_tags=risks,
            estimated_tokens=estimated_tokens,
            complexity=complexity,
            fingerprint=fingerprint,
            grouping_reason=draft.grouping_reason,
        )

    @staticmethod
    def _digest(value: Any) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    @staticmethod
    def _classify(item: ChangedFile) -> list[str]:
        path = item.file_path.casefold()
        name = PurePosixPath(path).name
        tags = {item.change_type}
        if item.old_file_path and item.old_file_path != item.file_path:
            tags.add("renamed")
        if DeterministicReviewPlanner._is_test_path(path):
            tags.add("test")
        if name in _GENERATED_NAMES or name.endswith(".lock"):
            tags.update({"dependency", "lockfile"})
        if name in _DEPENDENCY_MANIFESTS:
            tags.add("dependency")
        if path.startswith(".github/workflows/"):
            tags.update({"workflow", "config"})
        if re.search(r"(^|/)(migrations?|alembic/versions|db/migrate)(/|$)", path):
            tags.add("migration")
        if name in _CONFIG_NAMES or PurePosixPath(path).suffix in {".ini", ".cfg"}:
            tags.add("config")
        if re.search(r"(?:^|/)(?:dist|build|vendor|generated|__generated__)(?:/|$)", path):
            tags.add("generated")
        if PurePosixPath(path).suffix in _BINARY_EXTENSIONS:
            tags.add("binary")
        return sorted(tags)

    @staticmethod
    def _excluded_reason(path: str, tags: list[str]) -> str | None:
        if "binary" in tags:
            return "binary_file"
        if "generated" in tags and "dependency" not in tags:
            return "generated_file"
        return None

    @staticmethod
    def _rules(tags: list[str]) -> set[str]:
        rules = {"review.general"}
        mapping = {
            "test": "review.tests", "dependency": "review.dependencies",
            "migration": "review.migrations", "workflow": "review.workflows",
            "deleted": "review.deletions", "config": "review.configuration",
        }
        rules.update(rule for tag, rule in mapping.items() if tag in tags)
        return rules

    @staticmethod
    def _risks(path: str, tags: list[str]) -> set[str]:
        risks: set[str] = set()
        for tag in ("dependency", "migration", "workflow"):
            if tag in tags:
                risks.add(tag)
        lowered = path.casefold()
        for tag, words in {
            "auth": ("auth", "permission", "security"),
            "payment": ("payment", "billing", "invoice"),
            "concurrency": ("concurrent", "thread", "async", "lock"),
        }.items():
            if any(word in lowered for word in words):
                risks.add(tag)
        if "deleted" in tags:
            risks.add("deletion")
        return risks

    def _complexity(
        self, changed_lines: int, primary_files: list[str], risks: list[str]
    ) -> ReviewUnitComplexity:
        if changed_lines >= self.large_min_changed_lines or _HIGH_RISK_TAGS & set(risks):
            return ReviewUnitComplexity.large
        if changed_lines > self.small_max_changed_lines or len(primary_files) > 1 or risks:
            return ReviewUnitComplexity.medium
        return ReviewUnitComplexity.small

    @staticmethod
    def _is_test_path(path: str) -> bool:
        name = PurePosixPath(path).name
        return (
            bool(re.search(r"(^|/)(tests?|specs?)(/|$)", path))
            or name.startswith("test_")
            or bool(re.search(r"(?:\.test|\.spec)\.[^.]+$", name))
            or bool(re.search(r"_test\.[^.]+$", name))
        )

    @staticmethod
    def _test_key(path: str) -> tuple[str, str] | None:
        name = PurePosixPath(path).name
        suffix = PurePosixPath(name).suffix.casefold()
        stem = PurePosixPath(name).stem
        if stem.startswith("test_"):
            return stem[5:], suffix
        stem = re.sub(r"\.(?:test|spec)$", "", stem)
        if stem.endswith("_test"):
            stem = stem[:-5]
        return (stem, suffix) if DeterministicReviewPlanner._is_test_path(path.casefold()) else None

    @classmethod
    def _is_test_for(cls, test_path: str, source_path: str) -> bool:
        key = cls._test_key(test_path)
        source = PurePosixPath(source_path)
        return key is not None and key == (source.stem, source.suffix.casefold())

    @staticmethod
    def _resource_key(path: str) -> str | None:
        lowered = path.casefold()
        suffix = PurePosixPath(lowered).suffix
        if suffix not in _RESOURCE_EXTENSIONS or not re.search(r"(^|/)(locales?|i18n|l10n)(/|$)", lowered):
            return None
        parts = list(PurePosixPath(lowered).parts)
        marker = next((index for index, part in enumerate(parts) if part in {"locale", "locales", "i18n", "l10n"}), None)
        if marker is None:
            return None
        tail = parts[marker + 1:]
        if len(tail) > 1:
            tail = tail[1:]
        return "/".join(parts[:marker] + tail)

    @staticmethod
    def _changed_symbols(
        paths: list[str],
        changed_by_path: dict[str, ChangedFile],
        symbol_index: list[dict[str, Any]],
    ) -> list[str]:
        found: set[str] = set()
        for path in paths:
            item = changed_by_path[path]
            changed_lines = {
                line.line_no for hunk in item.hunks for line in hunk.added_lines if line.line_no
            }
            for symbol in symbol_index:
                if symbol.get("file") == path and any(
                    symbol.get("start_line", 0) <= line <= symbol.get("end_line", 0)
                    for line in changed_lines
                ):
                    found.add(str(symbol.get("symbol")))
            for hunk in item.hunks:
                for line in [*hunk.added_lines, *hunk.removed_lines]:
                    match = _PUBLIC_SYMBOL.match(line.content) or _ASSIGNMENT_SYMBOL.match(line.content)
                    if match:
                        found.add(match.group("name"))
        return sorted(found)

    @staticmethod
    def _related_files(primary: list[str], file_index: list[dict[str, Any]]) -> list[str]:
        if not file_index:
            return []
        by_stem: dict[str, list[str]] = defaultdict(list)
        by_path = {item.get("path"): item for item in file_index if item.get("path")}
        for path in by_path:
            by_stem[PurePosixPath(path).stem].append(path)
        related: set[str] = set()
        for path in primary:
            for imported in (by_path.get(path) or {}).get("imports", []):
                related.update(by_stem.get(str(imported).split(".")[0], []))
            for candidate in by_path:
                if DeterministicReviewPlanner._is_test_for(candidate, path):
                    related.add(candidate)
        return sorted(related - set(primary))[:8]

    @staticmethod
    def _validate_primary_ownership(units: list[ReviewUnit]) -> None:
        owners: dict[str, list[ReviewUnit]] = defaultdict(list)
        for unit in units:
            for path in unit.primary_files:
                owners[path].append(unit)
        accidental = {
            path: items for path, items in owners.items()
            if len(items) > 1 and any(item.grouping_reason != "large_file_hunk_split" for item in items)
        }
        if accidental:
            raise ValueError(f"primary files assigned to multiple units: {sorted(accidental)}")
