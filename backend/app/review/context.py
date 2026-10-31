"""Attempt-local references over shared immutable source bytes."""

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from app.review.domain import SourceRef
from app.review.evidence import source_lines
from app.review.planner import identity, members, tokens, unit_capacity
from app.review.store import BudgetExceeded


class NoProgressDetected(RuntimeError):
    """The stalled agent exhausted its bounded structured finalization attempts."""


@dataclass
class AgentContext:
    provider: Any
    work: Any
    budget: Any
    visible: dict[str, SourceRef] = field(default_factory=dict)
    pending: dict[str, SourceRef] = field(default_factory=dict)
    pending_hunks: dict[str, str] = field(default_factory=dict)
    material_failures: dict[str, str] = field(default_factory=dict)
    cursors: dict[str, dict] = field(default_factory=dict)
    tool_calls: int = 0
    model_calls: int = 0
    structured_output_retry: bool = False
    source_counter: int = 0
    tool_repetitions: dict[str, tuple[str, int]] = field(default_factory=dict)
    progress_keys: set[str] = field(default_factory=set)
    round_tools: int = 0
    round_progress: bool = False
    stalled_rounds: int = 0
    source_ranges: dict[tuple[str, str, str], list[tuple[int, int]]] = field(default_factory=dict)
    no_progress_closing_calls: int = 0
    investigation_closed: bool = False
    investigation_tools: set[str] = field(default_factory=set)
    source_paths: set[str] = field(default_factory=set)
    related_paths: set[str] = field(default_factory=set)
    delivered_hunks: set[str] = field(default_factory=set)
    coverage_checkpoint_sent: bool = False

    @property
    def evidence(self):
        return self.provider.evidence

    @property
    def denied_paths(self):
        return self.provider.denied_paths

    @property
    def changed_files(self):
        return [item.model_dump() for item in self.provider.run.changed_files]

    async def check_active(self):
        await self.budget.active()

    def note_source(self, source) -> bool:
        key = (source["commit_sha"], source["side"], source["path"])
        start, end = source["start_line"], source["end_line"]
        ranges = self.source_ranges.setdefault(key, [])
        fresh = not any(first <= start and end <= last for first, last in ranges)
        merged = []
        for first, last in sorted([*ranges, (start, end)]):
            if merged and first <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(last, merged[-1][1]))
            else:
                merged.append((first, last))
        self.source_ranges[key] = merged
        return fresh

    def note_tool_result(self, name, arguments, result) -> int:
        normalized = dict(arguments)
        cursor = normalized.get("cursor")
        if cursor in self.cursors:
            saved = self.cursors[cursor]
            normalized["cursor"] = {key: saved.get(key) for key in ("scope", "position", "column")}
        key = identity([name, normalized])
        # Cursors and attempt-local handles are opaque identities, not progress.
        def semantic(value):
            if isinstance(value, dict):
                return {key: semantic(item) for key, item in value.items()
                        if key not in {"cursor", "next_cursor", "source_ref"}}
            if isinstance(value, list):
                return [semantic(item) for item in value]
            return value
        digest = identity(semantic(result))
        progress = identity([key, digest])
        self.round_tools += 1
        if result.get("ok") is True:
            self.investigation_tools.add(name)
            if name == "search_code":
                self.related_paths.update(match["path"] for match in result.get("matches", []))
            elif name == "find_relations":
                self.related_paths.update(
                    edge[key].partition("::")[0] for edge in result.get("edges", [])
                    for key in ("source", "target") if isinstance(edge.get(key), str)
                )
        sources = ([result["source"]] if isinstance(result.get("source"), dict) else [])
        sources.extend(item["source"] for item in result.get("sources", [])
                       if isinstance(item, dict) and isinstance(item.get("source"), dict))
        fresh = False
        for source in sources:
            self.source_paths.add(source.get("path", ""))
            fresh = self.note_source(source) or fresh
        if (fresh if sources else progress not in self.progress_keys):
            self.progress_keys.add(progress)
            self.round_progress = True
        previous, count = self.tool_repetitions.get(key, (None, 0))
        count = count + 1 if previous == digest else 1
        self.tool_repetitions[key] = (digest, count)
        return count

    @staticmethod
    def is_test_path(path: str) -> bool:
        parts = PurePosixPath(path.casefold()).parts
        name = parts[-1] if parts else ""
        return (
            any(part in {"test", "tests", "__tests__", "spec", "specs"} for part in parts[:-1])
            or name.startswith("test_") or name.endswith(("_test.py", "_test.go"))
            or ".test." in name or ".spec." in name
        )

    def small_unit_ready_for_checkpoint(self) -> bool:
        if self.work.kind != "review":
            return False
        unit = next(unit for unit in self.provider.run.plan.units if unit.id == self.work.unit_id)
        changed_lines = sum(
            item.get("additions", 0) + item.get("deletions", 0)
            for item in self.changed_files if item["file_path"] in unit.paths
        )
        if len(unit.paths) > 3 or changed_lines >= 200:
            return False
        relation_checked = bool(self.investigation_tools & {
            "search_code", "find_relations",
        })
        materials_received = set(unit.required_hunk_ids) <= self.delivered_hunks
        tests_received = any(self.is_test_path(path) for path in self.source_paths)
        consumer_received = any(
            path in self.related_paths and path not in unit.paths and not self.is_test_path(path)
            for path in self.source_paths
        )
        return (
            materials_received
            and tests_received
            and relation_checked
            and consumer_received
        )

    @staticmethod
    def _source_items(value):
        if isinstance(value, dict):
            source = value.get("source")
            if isinstance(source, dict) and isinstance(value.get("content"), str):
                try:
                    SourceRef.model_validate(source)
                except ValueError:
                    pass
                else:
                    yield value, source
            for item in value.values():
                yield from AgentContext._source_items(item)
        elif isinstance(value, list):
            for item in value:
                yield from AgentContext._source_items(item)

    @staticmethod
    def _source_range(source):
        return (
            source.get("commit_sha"), source.get("side"), source.get("path"),
            source.get("start_line"), source.get("end_line"),
        )

    @classmethod
    def compact_messages(cls, messages):
        """Omit duplicate source bodies while preserving handles and fixed ranges."""
        ranges = {}
        tool_results = {}
        compacted = []
        for message in messages:
            if message.type not in {"human", "tool"} or not isinstance(message.content, str):
                compacted.append(message)
                continue
            try:
                payload = json.loads(message.content)
            except (TypeError, ValueError):
                compacted.append(message)
                continue
            changed = False
            for item, source in cls._source_items(payload):
                key = cls._source_range(source)[:3]
                start, end = source.get("start_line"), source.get("end_line")
                known = ranges.setdefault(key, [])
                duplicate = any(first <= start and end <= last for first, last in known)
                if duplicate:
                    item["content"] = (
                        f"[source body omitted; use evidence {item.get('source_ref', source.get('id'))} "
                        f"for {source.get('path')}:{start}-{end}]"
                    )
                    item["content_omitted"] = True
                    changed = True
                merged = []
                for first, last in sorted([*known, (start, end)]):
                    if merged and first <= merged[-1][1] + 1:
                        merged[-1] = (merged[-1][0], max(last, merged[-1][1]))
                    else:
                        merged.append((first, last))
                ranges[key] = merged
                if message.type == "tool":
                    item["source"] = {key: source[key] for key in (
                        "commit_sha", "side", "path", "start_line", "end_line",
                    )}
                    changed = True
            if message.type == "tool" and isinstance(payload, dict):
                fingerprint = identity([message.name, payload])
                previous = tool_results.get(fingerprint)
                if previous and any(key in payload for key in ("matches", "edges", "paths", "member")):
                    payload = {key: value for key, value in payload.items()
                               if key not in {"matches", "edges", "paths", "member"}}
                    payload["same_result_as_tool_call"] = previous
                    payload["content_omitted"] = True
                    changed = True
                else:
                    tool_results[fingerprint] = message.tool_call_id
            compacted.append(message.model_copy(update={
                "content": json.dumps(payload, ensure_ascii=False),
            }) if changed else message)
        return compacted

    def begin_round(self):
        if self.round_tools:
            self.stalled_rounds = 0 if self.round_progress else self.stalled_rounds + 1
        self.round_tools, self.round_progress = 0, False
        return self.stalled_rounds

    def stage(self, ref: SourceRef):
        for handle, issued in {**self.visible, **self.pending}.items():
            if issued.id == ref.id:
                return handle
        self.source_counter += 1
        handle = f"S{self.source_counter}"
        self.pending[handle] = ref
        return handle

    def resolve(self, handles):
        if len(set(handles)) != len(handles) or any(handle not in self.visible for handle in handles):
            raise ValueError("source_not_visible_in_this_attempt")
        return [self.visible[handle] for handle in handles]

    async def acknowledge(self, pending, hunks):
        await asyncio.to_thread(self.provider.store.deliver, self.provider.run.id, self.work,
            list(hunks), self.material_failures, self.provider.lease,
            source_ids=[ref.id for ref in pending.values()], digests=hunks)
        self.visible.update(pending)
        self.delivered_hunks.update(hunks)
        for ref in pending.values():
            self.source_paths.add(ref.path)
            self.note_source(ref.model_dump())
        for handle in pending:
            self.pending.pop(handle, None)
        for key in hunks:
            self.pending_hunks.pop(key, None)

    async def read(self, path, start, end, revision):
        result = await self.evidence.read(path, start, end, revision)
        ref = SourceRef.model_validate(result.pop("evidence"))
        handle = self.stage(ref)
        return {**result, "source_ref": handle, "source": ref.model_dump()}


@dataclass
class ContextProvider:
    run: Any
    evidence: Any
    store: Any
    lease: dict
    denied_paths: frozenset[str] = frozenset()
    discussions: list[dict] = field(default_factory=list)
    index: Any = None

    def material_capacity(self) -> int:
        return unit_capacity(self.run.configuration.options.limits)

    def _member(self, key):
        return members(self.run.changed_files)[key]

    async def material(self, context, key):
        member = self._member(key)
        references = []
        for side, number_key, path in (
            ("base", "old_line_no", member["old_path"]),
            ("head", "new_line_no", member["path"]),
        ):
            lines = [line for line in member["lines"] if line[number_key] is not None]
            if not lines:
                continue
            first, last = lines[0][number_key], lines[-1][number_key]
            result = await self.evidence.read(path, first, last, side)
            actual = source_lines(result["content"])
            for line in lines:
                if actual[line[number_key] - first].rstrip("\n") != line["content"]:
                    raise ValueError("diff_blob_mismatch")
            ref = SourceRef.model_validate(result["evidence"])
            references.append((ref, result["content"]))
        if not member["lines"]:
            git, repo, snap = self.evidence.git, self.evidence.repo_path, self.run.snapshot
            before = await asyncio.to_thread(git.get_tree_entries, repo, snap.base_sha, [member["old_path"]])
            after = await asyncio.to_thread(git.get_tree_entries, repo, snap.head_sha, [member["path"]])
            if not before and not after:
                raise ValueError("structural_member_unavailable")
            member = {**member, "base_entry": before, "head_entry": after}
        packet = {"member": member, "sources": [
            {"source": ref.model_dump(), "content": content} for ref, content in references]}
        if tokens(packet) > self.material_capacity():
            raise ValueError("material_capacity")
        for item, (ref, _) in zip(packet["sources"], references):
            item["source_ref"] = context.stage(ref)
        context.pending_hunks[key] = identity(packet)
        return packet

    async def initial(self, context, candidate=None, *, request_budget=None, estimate_request=None):
        unit = next(unit for unit in self.run.plan.units if unit.id == context.work.unit_id)
        saved = await asyncio.to_thread(self.store.candidates, self.run.id)
        material, candidate_materials = [], []
        missing_candidate_materials = [
            {"path": ref.path, "side": ref.side, "start_line": ref.start_line,
             "end_line": ref.end_line, "reason": "material_capacity"}
            for ref in candidate.sources] if candidate else []
        omissions = list(missing_candidate_materials)
        context.material_failures.update({key: "material_capacity" for key in unit.required_hunk_ids})
        base = {
            "snapshot": self.run.snapshot.model_dump(), "depth": self.run.configuration.options.depth,
            "focus": unit.focus,
            "change_map": [{"paths": item.paths, "risk": item.risk} for item in self.run.plan.units][:100],
            "history_untrusted": [{"claim": item["claim"], "head_sha": item["head_sha"],
                "status": "historical_not_current_evidence"} for item in self.run.plan.history[:20]],
            "author_statement_untrusted": {"title": self.run.pr.title, "body": (self.run.pr.body or "")[:4000]},
            "unit": unit.model_dump(), "materials": material, "missing_material": context.material_failures,
            "already_submitted": [{"id": item.id, "claim": item.proposal.claim}
                                  for item in saved if item.unit_id == unit.id][:100],
            "candidate": candidate.proposal.model_dump(exclude={"source_refs"}) if candidate else None,
            "candidate_materials": candidate_materials,
            "missing_candidate_materials": missing_candidate_materials,
        }
        def fits_request():
            return request_budget is None or estimate_request(base) <= request_budget

        # Account for omission metadata before admitting any source material.
        if request_budget is not None:
            for key in ("already_submitted", "history_untrusted", "change_map"):
                while base[key] and not fits_request():
                    base[key] = base[key][:len(base[key]) // 2]
            for key in ("body", "title"):
                if not fits_request():
                    base["author_statement_untrusted"][key] = ""
            if not fits_request():
                raise BudgetExceeded("input_token_limit")
        spent = 0
        if candidate:
            for ref, omission in zip(candidate.sources, omissions):
                before_pending = dict(context.pending)
                try:
                    item = await context.read(ref.path, ref.start_line, ref.end_line, ref.side)
                except (ValueError, OSError):
                    omission["reason"] = "material_unavailable"
                    continue
                cost = tokens(item)
                candidate_materials.append(item)
                missing_candidate_materials.remove(omission)
                if ((request_budget is None and spent + cost > self.material_capacity())
                        or not fits_request()):
                    candidate_materials.pop()
                    context.pending = before_pending
                    missing_candidate_materials.append(omission)
                    continue
                spent += cost
        by_path = {}
        member_map = members(self.run.changed_files)
        for key in unit.required_hunk_ids:
            by_path.setdefault(member_map[key]["path"], []).append(key)
        # Give every related file a chance before consuming the remaining space.
        ordered_keys = [keys[index] for index in range(max(map(len, by_path.values()), default=0))
                        for keys in by_path.values() if index < len(keys)]
        for key in ordered_keys:
            before_pending = dict(context.pending)
            try:
                packet = await self.material(context, key)
            except (ValueError, OSError):
                context.material_failures[key] = "material_unavailable"
                continue
            cost = tokens(packet)
            material.append(packet)
            context.material_failures.pop(key, None)
            if ((request_budget is None and spent + cost > self.material_capacity())
                    or not fits_request()):
                material.pop()
                context.pending_hunks.pop(key, None)
                context.pending = before_pending
                context.material_failures[key] = "material_capacity"
                continue
            spent += cost
        if not fits_request():
            raise BudgetExceeded("input_token_limit")
        return base
