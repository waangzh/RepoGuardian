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
from app.services.fingerprints import unit_fingerprint
from app.models.review import (
    ChangedFile,
    ContextProvenance,
    ExcludedReviewFile,
    PlannedChangedFile,
    ReviewPlan,
    ReviewToolScope,
    ReviewUnit,
    ReviewUnitComplexity,
)
from app.tools.diff_parser import stable_hunk_id
from app.review.language_rules import language_for_path
from app.review.tool_scope import is_sensitive_repository_change

PLANNER_VERSION = "review-unit-planner-v3"

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
        repository_graph: dict[str, Any] | None = None,
        model: str | None = None,
        provider: str | None = None,
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
            reason = self._excluded_reason(
                item.file_path, tags, old_file_path=item.old_file_path
            )
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

        drafts = self._group_files(included, classified, file_index or [], symbol_index or [])
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
                repository_graph or {},
                model or settings.repoguardian_model,
                provider or settings.repoguardian_provider,
            )
            for draft in drafts
        ]
        units.sort(
            key=lambda unit: (
                unit.primary_files, unit.diff_hunk_ids, unit.changed_symbols, unit.id
            )
        )
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

    def build_scope(
        self,
        unit: ReviewUnit,
        repository_root: str | None = None,
        repository_files: set[str] | None = None,
    ) -> ReviewToolScope:
        provenance = {
            path: ContextProvenance(
                file=path,
                source="changed_symbol" if unit.changed_symbols else "changed_file",
                distance=0,
                confidence=1.0,
                why_retrieved="primary file changed in this review unit",
                unit_id=unit.id,
            )
            for path in unit.primary_files
        }
        provenance.update({item.file: item for item in unit.context_provenance})
        seed_files = set(unit.primary_files) | set(unit.related_files)
        readable_files = set(repository_files or seed_files)
        readable_files.update(seed_files)
        return ReviewToolScope(
            review_unit_id=unit.id,
            commentable_files=set(unit.primary_files),
            seed_files=seed_files,
            readable_files=readable_files,
            repository_discovery_enabled=True,
            context_provenance=provenance,
            repository_root=repository_root,
            max_lines_per_read=self.max_lines_per_read,
            max_search_results=self.max_search_results,
            max_context_chars={
                ReviewUnitComplexity.small: 30_000,
                ReviewUnitComplexity.medium: 60_000,
                ReviewUnitComplexity.large: 100_000,
            }[unit.complexity],
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

    def planning_model_calls(
        self, unit: ReviewUnit, changed_files: Iterable[ChangedFile]
    ) -> int:
        return 0 if self.should_skip_plan(unit, changed_files) else 1

    def estimated_model_calls(
        self, unit: ReviewUnit, changed_files: Iterable[ChangedFile]
    ) -> int:
        """估算 Plan、一次诊断和必要决策；不把预算上限误报为典型成本。"""
        planning = self.planning_model_calls(unit, changed_files)
        decision_calls = 1 if unit.complexity == ReviewUnitComplexity.small else 2
        diagnosis_calls = 1
        return planning + decision_calls + diagnosis_calls

    @staticmethod
    def max_model_calls(unit: ReviewUnit) -> int:
        if unit.complexity == ReviewUnitComplexity.small:
            return 3
        if unit.complexity == ReviewUnitComplexity.medium:
            return 5
        return 7

    @staticmethod
    def hunk_id(file_path: str, index: int, hunk: dict[str, Any]) -> str:
        del index  # 兼容旧调用签名；稳定 ID 不依赖 hunk 在文件中的序号。
        if hunk.get("hunk_id"):
            return str(hunk["hunk_id"])
        return stable_hunk_id(
            file_path,
            int(hunk.get("old_start", 0)),
            int(hunk.get("old_length", 0)),
            int(hunk.get("new_start", 0)),
            int(hunk.get("new_length", 0)),
            list(hunk.get("lines") or []),
        )

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
        file_index: list[dict[str, Any]],
        symbol_index: list[dict[str, Any]],
    ) -> list[_UnitDraft]:
        drafts: list[_UnitDraft] = []
        remaining = {item.file_path: item for item in files}

        deletions = sorted(path for path, item in remaining.items() if item.change_type == "deleted")
        if deletions:
            drafts.append(_UnitDraft(tuple(deletions), "deletion_group"))
            for path in deletions:
                remaining.pop(path)

        self._append_related_pairs(
            drafts, remaining, classified, file_index, "migration", "migration_with_model"
        )
        self._append_related_pairs(
            drafts, remaining, classified, file_index, "api", "api_with_model"
        )

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
            if changed_lines >= self.large_min_changed_lines:
                changed_symbols = self._changed_symbols([path], {path: item}, symbol_index)
                if len(changed_symbols) > 1:
                    drafts.extend(
                        _UnitDraft(
                            (path,), "large_file_symbol_split", ids, (symbol,)
                        )
                        for symbol in changed_symbols
                    )
                elif len(ids) > 1:
                    drafts.extend(
                        _UnitDraft((path,), "large_file_hunk_split", (hid,)) for hid in ids
                    )
                else:
                    drafts.append(_UnitDraft((path,), "single_file"))
            else:
                drafts.append(_UnitDraft((path,), "single_file"))
        return drafts

    @classmethod
    def _append_related_pairs(
        cls,
        drafts: list[_UnitDraft],
        remaining: dict[str, ChangedFile],
        classified: dict[str, list[str]],
        file_index: list[dict[str, Any]],
        source_tag: str,
        reason: str,
    ) -> None:
        """按 import 和实体名确定性配对 API/migration 与直接 model。"""
        indexed = {item.get("path"): item for item in file_index if item.get("path")}
        source_paths = sorted(path for path in remaining if source_tag in classified[path])
        model_paths = sorted(path for path in remaining if "model" in classified[path])
        for source in source_paths:
            if source not in remaining:
                continue
            imports = {
                str(item).casefold().split(".")[-1]
                for item in (indexed.get(source) or {}).get("imports", [])
            }
            source_tokens = cls._entity_tokens(source)
            ranked: list[tuple[int, str]] = []
            for model in model_paths:
                if model not in remaining or model == source:
                    continue
                model_stem = PurePosixPath(model).stem.casefold()
                shared = source_tokens & cls._entity_tokens(model)
                score = (4 if model_stem in imports else 0) + len(shared)
                if score > 0:
                    ranked.append((score, model))
            if ranked:
                model = sorted(ranked, key=lambda item: (-item[0], item[1]))[0][1]
                drafts.append(_UnitDraft((source, model), reason))
                remaining.pop(source)
                remaining.pop(model)

    @staticmethod
    def _entity_tokens(path: str) -> set[str]:
        ignored = {
            "add", "alter", "api", "app", "backend", "controller", "create", "db",
            "frontend", "handler", "lib", "migration", "migrations", "model", "models",
            "remove", "route", "routes", "schema", "schemas", "src", "table", "update",
            "version", "versions",
        }
        tokens = {
            token for token in re.split(r"[^a-z0-9]+", path.casefold())
            if len(token) > 2 and not token.isdigit() and token not in ignored
        }
        return tokens | {token[:-1] for token in tokens if token.endswith("s") and len(token) > 3}

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
        repository_graph: dict[str, Any],
        model: str,
        provider: str,
    ) -> ReviewUnit:
        primary = list(draft.primary_files)
        selected_hunks = list(draft.hunk_ids or (
            hid for path in primary for hid in all_hunk_ids.get(path, [])
        ))
        symbols = self._changed_symbols(primary, changed_by_path, symbol_index)
        if draft.symbol_filter:
            symbols = [symbol for symbol in symbols if symbol in set(draft.symbol_filter)]
        rules = sorted({rule for path in primary for rule in self._rules(classified[path])})
        rules = sorted({
            *rules,
            *(
                f"review.language.{language}"
                for path in primary
                if (language := language_for_path(path, file_index)) != "unknown"
            ),
        })
        risks = sorted({risk for path in primary for risk in self._risks(path, classified[path])})
        if symbols and any("test" not in classified[path] for path in primary):
            risks.append("public_api")
        if len({PurePosixPath(path).parent for path in primary}) > 1:
            risks.append("cross_module")
        risks = sorted(set(risks))
        related_context = self._related_context(primary, file_index, repository_graph)
        related = [item.file for item in related_context]
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
            "symbols": symbols,
            "planner_version": PLANNER_VERSION,
        }
        unit_id = "ru-" + self._digest(identity)[:16]
        related_context = [item.model_copy(update={"unit_id": unit_id}) for item in related_context]
        fingerprint = unit_fingerprint(
            base_sha=base_sha,
            head_sha=head_sha,
            normalized_unit_diff=normalized,
            primary_files=primary,
            related_files=related,
            rule_ids=rules,
            rule_version=settings.repoguardian_rule_version,
            prompt_version=settings.repoguardian_prompt_version,
            tool_schema_version=settings.repoguardian_tool_schema_version,
            planner_version=PLANNER_VERSION,
            review_policy_version=settings.repoguardian_review_policy_version,
            model=model,
            provider=provider,
        )
        return ReviewUnit(
            id=unit_id,
            primary_files=primary,
            related_files=related,
            context_provenance=related_context,
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
        parts = set(PurePosixPath(path).parts)
        if parts & {"api", "routes", "routers", "controllers", "handlers"}:
            tags.add("api")
        if parts & {"models", "schemas", "dto", "types"} or re.search(
            r"(?:_model|_schema|_dto)$", PurePosixPath(path).stem
        ):
            tags.add("model")
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
    def _excluded_reason(
        path: str, tags: list[str], *, old_file_path: str | None = None
    ) -> str | None:
        if is_sensitive_repository_change(path, old_file_path):
            return "sensitive_file"
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
            "api": "review.api", "model": "review.models",
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
        return [
            item.file
            for item in DeterministicReviewPlanner._related_context(primary, file_index, {})
        ]

    @staticmethod
    def _related_context(
        primary: list[str],
        file_index: list[dict[str, Any]],
        repository_graph: dict[str, Any],
    ) -> list[ContextProvenance]:
        """按距离、证据类型和置信度生成不可扩张的分层可读范围。"""
        if not file_index:
            return []
        from app.tools.repository_graph import build_repository_graph

        graph = repository_graph or build_repository_graph(file_index, [])
        primary_set = set(primary)
        candidates: dict[str, ContextProvenance] = {}
        source_order = {
            "caller": 0, "callee": 1, "test": 2, "import": 3,
            "importer": 4, "implementation": 5, "dependency": 6, "config": 7,
        }

        def offer(path: str, source: str, distance: int, confidence: float, why: str) -> None:
            if path in primary_set or path not in {item.get("path") for item in file_index}:
                return
            item = ContextProvenance(
                file=path,
                source=source,
                distance=distance,
                confidence=confidence,
                why_retrieved=why,
            )
            current = candidates.get(path)
            if current is None or (
                distance, source_order.get(source, 99), -confidence, source
            ) < (
                current.distance, source_order.get(current.source, 99),
                -current.confidence, current.source
            ):
                candidates[path] = item

        for edge in graph.get("edges", []):
            kind = str(edge.get("type", ""))
            source = str(edge.get("source", ""))
            target = str(edge.get("target", ""))
            source_file = str(edge.get("source_file") or source.split("::", 1)[0])
            target_file = str(edge.get("target_file") or target.split("::", 1)[0])
            confidence = float(edge.get("confidence", 0))
            why = str(edge.get("why", kind))
            if kind == "imports":
                if source_file in primary_set:
                    offer(target_file, "import", 1, confidence, why)
                if target_file in primary_set:
                    source_path = PurePosixPath(source_file.casefold())
                    is_test_importer = (
                        any(part in {"test", "tests", "testing", "__tests__"} for part in source_path.parts)
                        or source_path.name.startswith("test_")
                        or any(token in source_path.name for token in (".test.", ".spec."))
                    )
                    offer(source_file, "test" if is_test_importer else "importer", 1, confidence, why)
            elif kind == "calls":
                if source_file in primary_set:
                    offer(target_file, "callee", 1, confidence, why)
                if target_file in primary_set:
                    offer(source_file, "caller", 1, confidence, why)
            elif kind == "test_of":
                if target_file in primary_set:
                    offer(source_file, "test", 1, confidence, why)
                if source_file in primary_set:
                    offer(target_file, "implementation", 1, confidence, why)
            elif kind == "configures" and target_file in primary_set:
                offer(source_file, "config", 2, confidence, why)

        level_one = {path for path, item in candidates.items() if item.distance == 1}
        for edge in graph.get("edges", []):
            if edge.get("type") != "imports" or float(edge.get("confidence", 0)) < 0.85:
                continue
            source = str(edge.get("source", ""))
            target = str(edge.get("target", ""))
            if source in level_one:
                offer(
                    target, "dependency", 2, float(edge["confidence"]) * 0.85,
                    f"transitive dependency via {source}: {edge.get('why', 'imports')}",
                )

        ranked = sorted(
            candidates.values(),
            key=lambda item: (
                item.distance, source_order.get(item.source, 99),
                -item.confidence, item.file,
            ),
        )
        return ranked[:12]

    @staticmethod
    def _validate_primary_ownership(units: list[ReviewUnit]) -> None:
        owners: dict[str, list[ReviewUnit]] = defaultdict(list)
        for unit in units:
            for path in unit.primary_files:
                owners[path].append(unit)
        accidental = {
            path: items for path, items in owners.items()
            if len(items) > 1 and any(
                item.grouping_reason not in {"large_file_hunk_split", "large_file_symbol_split"}
                for item in items
            )
        }
        if accidental:
            raise ValueError(f"primary files assigned to multiple units: {sorted(accidental)}")
