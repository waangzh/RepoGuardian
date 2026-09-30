"""只读、确定性的跨 Unit 风险筛查；本阶段不调用模型、不执行补查。"""

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any

from app.models.review import (
    CrossUnitRelationship, CrossUnitRiskAssessment, CrossUnitRiskReason,
    ReviewUnit, ReviewUnitResult,
    ChangedFile,
)
from app.review.unit_completion import is_review_unit_complete
from app.services.review_planner import DeterministicReviewPlanner


_CONTRACT_TAGS = {"public_api", "api", "model", "config", "migration", "auth", "permission", "security"}
_CONTRACT_PARTS = {"api", "apis", "schema", "schemas", "model", "models", "types", "config", "configs",
                   "migration", "migrations", "auth", "permissions"}
_PASSIVE_SUFFIXES = {".md", ".txt", ".rst", ".png", ".jpg", ".svg", ".css", ".scss"}
_CONTRACT_CHANGE = re.compile(
    r"^\s*(?:async\s+def\b|def\b|class\b|export\b|interface\b|type\s+\w+\s*=|"
    r"return\b|@(?:\w+\.)?(?:get|post|put|patch|delete|route)\b|"
    r"[A-Za-z_]\w*\??\s*:\s*[A-Za-z_]|(?:public|protected)\s+)"
)


def _contract_unit(unit: ReviewUnit) -> bool:
    return bool(_CONTRACT_TAGS & set(unit.risk_tags)) or any(
        _CONTRACT_PARTS & set(PurePosixPath(path).with_suffix("").parts)
        for path in unit.primary_files
    )


def _passive_unit(unit: ReviewUnit) -> bool:
    return not _contract_unit(unit) and all(
        PurePosixPath(path).suffix.lower() in _PASSIVE_SUFFIXES for path in unit.primary_files
    )


class CrossUnitRiskService:
    def assess(self, state: dict[str, Any]) -> CrossUnitRiskAssessment:
        units = sorted((ReviewUnit.model_validate(item) for item in state.get("review_units") or []),
                       key=lambda item: item.id)
        results = {item.review_unit_id: item for item in (
            ReviewUnitResult.model_validate(raw) for raw in state.get("review_unit_results") or []
        )}
        graph = state.get("repository_graph") or {}
        file_index = {item["path"]: item for item in state.get("file_index") or [] if item.get("path")}
        owners: dict[str, list[ReviewUnit]] = {}
        for unit in units:
            for path in unit.primary_files:
                owners.setdefault(path, []).append(unit)
        reasons: list[CrossUnitRiskReason] = []
        required = False
        uncertain = False
        relationships: dict[str, CrossUnitRelationship] = {}
        contract_changes: set[str] = set()
        changed_files = {item.file_path: item for item in (
            ChangedFile.model_validate(raw) for raw in state.get("changed_files") or []
        )}
        for unit in units:
            selected = set(unit.diff_hunk_ids)
            for path in unit.primary_files:
                item = changed_files.get(path)
                if item is None:
                    continue
                lines = [line for index, hunk in enumerate(item.hunks)
                         if not selected or DeterministicReviewPlanner.hunk_id(
                             path, index, hunk.model_dump(mode="json")
                         ) in selected
                         for line in [*hunk.added_lines, *hunk.removed_lines]]
                if any(_CONTRACT_CHANGE.search(line.content) for line in lines):
                    contract_changes.add(unit.id)

        def add(code: str, involved: list[str], files: list[str], detail: str,
                evidence: list[str] | None = None, *, needs_check: bool = True) -> None:
            nonlocal required, uncertain
            required = required or needs_check
            uncertain = uncertain or not needs_check
            reasons.append(CrossUnitRiskReason(
                code=code, unit_ids=sorted(set(involved)), files=sorted(set(files)),
                evidence_ids=sorted(set(evidence or [])), detail=detail,
            ))

        def relation(source: ReviewUnit, target: ReviewUnit, source_file: str, target_file: str,
                     kind: str, confidence: float, source_symbol: str | None = None,
                     target_symbol: str | None = None, parser_id: str | None = None,
                     provenance: str = "") -> str:
            identity = json.dumps([source.id, target.id, source_file, target_file, kind,
                                   source_symbol, target_symbol], ensure_ascii=False)
            key = "relation-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
            relationships[key] = CrossUnitRelationship(
                id=key, source_unit_id=source.id, target_unit_id=target.id,
                source_file=source_file, target_file=target_file, type=kind,
                confidence=max(0, min(1, confidence)), source_symbol=source_symbol,
                target_symbol=target_symbol,
                parser_id=parser_id, provenance=provenance,
            )
            return key

        indexed_paths = set(file_index)
        eligible_paths = set(owners)
        metadata = graph.get("metadata") or {}
        graph_paths = {item.get("path") for item in graph.get("files") or []}
        index_available = bool(graph) and eligible_paths <= indexed_paths and eligible_paths <= graph_paths
        index_available = index_available and metadata.get("file_count") == len(file_index)
        index_available = index_available and metadata.get("edge_count") == len(graph.get("edges") or [])
        index_available = index_available and all(
            int(file_index[path].get("analysis_level") or 0) >= 2
            for path in eligible_paths if PurePosixPath(path).suffix.lower() not in _PASSIVE_SUFFIXES
        )
        index_status = "available" if index_available else "partial" if graph or file_index else "unknown"
        if len(units) < 2:
            return CrossUnitRiskAssessment(decision="skip", index_status=index_status, reasons=[
                CrossUnitRiskReason(code="no_cross_unit_scope", unit_ids=[item.id for item in units],
                                    detail="当前计划不足两个 Unit，跨 Unit 筛查不适用；局部覆盖另行统计")
            ])

        for edge in graph.get("edges") or []:
            kind = edge.get("type")
            if kind not in {"imports", "calls", "test_of", "configures"}:
                continue
            source_ref, target_ref = str(edge.get("source") or ""), str(edge.get("target") or "")
            source_file = str(edge.get("source_file") or source_ref.split("::", 1)[0])
            target_file = str(edge.get("target_file") or target_ref.split("::", 1)[0])
            source_symbol = source_ref.split("::", 1)[1] if "::" in source_ref else None
            target_symbol = target_ref.split("::", 1)[1] if "::" in target_ref else None
            for source in owners.get(source_file, []):
                for target in owners.get(target_file, []):
                    if source.id == target.id:
                        continue
                    if source_file == target_file and (
                        kind != "calls" or source_symbol not in source.changed_symbols
                        or target_symbol not in target.changed_symbols
                    ):
                        continue
                    if kind == "calls" and source.changed_symbols and source_symbol not in source.changed_symbols:
                        continue
                    if kind == "calls" and target.changed_symbols and target_symbol not in target.changed_symbols:
                        continue
                    evidence = relation(source, target, source_file, target_file, kind,
                                        float(edge.get("confidence") or 0), source_symbol, target_symbol,
                                        edge.get("parser_id"), str(edge.get("why") or ""))
                    involved = [source.id, target.id]
                    files = [source_file, target_file]
                    # 解析可信度只用于关系分层，不是缺陷概率。
                    semantic_edge = (
                        kind in {"calls", "imports"}
                        and float(edge.get("confidence") or 0) >= 0.85
                        and str(edge.get("parser_id") or "").startswith("tree-sitter.")
                    )
                    if not semantic_edge and kind != "test_of":
                        add("relationship_unknown", involved, files,
                            "关系来自启发式或较弱解析证据，需要先判别真实关联", [evidence], needs_check=False)
                        continue
                    if semantic_edge and any(
                        not results.get(item) or not is_review_unit_complete(results[item]) for item in involved
                    ):
                        add("dependency_coverage_gap", involved, files,
                            "跨 Unit 关系所依赖的局部审查未完整完成", [evidence])
                    if semantic_edge and set(involved) & contract_changes and (
                        kind == "calls" or _contract_unit(source) or _contract_unit(target)
                    ):
                        add("changed_contract", involved, files,
                            "不同 Unit 的变更存在调用或契约导入关系，需要核验组合行为", [
                                evidence, *(reference.id for identity in involved if identity in results
                                            for reference in results[identity].review_summary.evidence
                                            if reference.source == "diff"),
                            ])

        for unit in units:
            result = results.get(unit.id)
            record = result.review_summary.record if result and result.review_summary.status == "reported" else None
            if record is None:
                if not _passive_unit(unit):
                    add("summary_unknown", [unit.id], unit.primary_files,
                        "缺少可信检查记录，不能从零 Issue 或执行结束推断语义覆盖", needs_check=False)
                continue
            catalog = {item.id for item in result.review_summary.evidence}
            for dependency in record.contract_dependencies:
                for target in owners.get(dependency.file_path, []):
                    if target.id == unit.id:
                        continue
                    relation(unit, target, unit.primary_files[0], dependency.file_path,
                             "declared_dependency", 0.0, target_symbol=dependency.symbol,
                             provenance="Unit 声明的待核验依赖，引用与路径已由执行器检查")
                    evidence = [item for item in dependency.evidence_ids if item in catalog]
                    if dependency.status in {"unresolved", "conflicting"}:
                        add("conflicting_contract" if dependency.status == "conflicting" else "unresolved_dependency",
                            [unit.id, target.id], [dependency.file_path], dependency.assumption, evidence)
                    if not results.get(target.id) or not is_review_unit_complete(results[target.id]):
                        add("dependency_coverage_gap", [unit.id, target.id], [dependency.file_path],
                            "声明依赖的 Unit 未完整完成，依赖条件仍缺少覆盖", evidence)
            for question in record.unresolved_questions:
                related = {other.id for path in question.affected_files for other in owners.get(path, [])}
                if related - {unit.id}:
                    add("unresolved_cross_unit_question", [unit.id, *related], question.affected_files,
                        question.question, [item for item in question.evidence_ids if item in catalog])
                elif not question.affected_files:
                    add("relationship_unknown", [unit.id], unit.primary_files,
                        "未决问题尚未明确影响范围：" + question.question, needs_check=False)
            if (
                any(item.status != "checked" for item in record.target_checks)
                or any(item.status == "unresolved" for item in record.hypothesis_checks)
            ):
                add("relationship_unknown", [unit.id], unit.primary_files,
                    "检查目标或风险假设仍未完成，不能据此确认变更独立", needs_check=False)

        related_unit_ids = {item.source_unit_id for item in relationships.values()} | {
            item.target_unit_id for item in relationships.values()
        }
        for unit in units:
            if _contract_unit(unit) and unit.id not in related_unit_ids:
                add("relationship_unknown", [unit.id], unit.primary_files,
                    "涉及契约变更但未建立跨 Unit 关系；无图边不等于无依赖", needs_check=False)
        if not index_available and any(not _passive_unit(item) for item in units):
            add("relationship_unknown", [item.id for item in units], sorted(eligible_paths),
                "索引缺失、降级或内部计数不自洽，无法充分判别变更关系", needs_check=False)
        # 未完成的局部任务只影响局部覆盖；没有依赖关系时不自动升级 required。
        if not required and not uncertain:
            reasons.append(CrossUnitRiskReason(
                code="independent_changes", unit_ids=[item.id for item in units],
                files=sorted(eligible_paths), detail="当前规则未发现跨 Unit 风险；这不是代码正确性证明",
            ))
        unique = {item.model_dump_json(): item for item in reasons}
        decision = "required" if required else "uncertain" if uncertain else "skip"
        return CrossUnitRiskAssessment(
            decision=decision, index_status=index_status,
            reasons=[unique[key] for key in sorted(unique)],
            relationships=[relationships[key] for key in sorted(relationships)],
            execution_status="not_implemented" if decision != "skip" else "not_requested",
            non_execution_reason="阶段 A/B 仅完成风险筛查，协调与补查将在阶段 C 接入" if decision != "skip" else None,
        )
