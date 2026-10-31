import json
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from app.models.review.diff import ChangedFile
from app.review.budget import BudgetGate
from app.review.domain import Report, Run, WorkItem, utcnow
from app.review.overview_materials import MATERIAL_TOKENS, overview_material
from app.review.planner import members, plan_review, tokens
from app.review.report import build_report, generate_overview
from app.review.store import OVERVIEW_INPUT, BudgetExceeded, LeaseLost, ReviewStopped
from app.services.publication.publication_planner import PublicationPlanner
from tests.review.test_orchestrator import ReviewModel
from tests.review.vnext_helpers import setup_source_run


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True).stdout.decode().strip()


def commit(repo, message):
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def onpar_run(tmp_path):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    fixture = json.loads(Path(__file__).with_name("fixtures").joinpath(
        "onpar_95_synthesis.json").read_text(encoding="utf-8"))
    run.changed_files = [ChangedFile.model_validate(item) for item in fixture["changed_files"]]
    repo = provider.evidence.repo_path
    # Store frozen blobs from fixture texts; never execute the target code.
    for side, key in (("base", "old_line_no"), ("head", "new_line_no")):
        for change in run.changed_files:
            lines = ["# existing background\n"] * 620
            for hunk in change.hunks:
                for line in hunk.lines:
                    number = getattr(line, key)
                    if number is not None:
                        lines[number - 1] = line.content + "\n"
            target = repo / change.file_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("".join(lines), encoding="utf-8")
        (repo / "caller.py").write_text("# unrelated caller\n" * 1000, encoding="utf-8")
        sha = commit(repo, side)
        if side == "base":
            run.snapshot.base_sha = run.snapshot.declared_base_sha = run.pr.base.sha = sha
        else:
            run.snapshot.head_sha = run.pr.head.sha = sha
    run.plan = plan_review(run.changed_files, run.snapshot, run.configuration.options)
    provider.evidence.allowed_paths = frozenset([*run.plan.units[0].paths, "caller.py"])
    report = Report(version="d" * 64, status="completed", findings=[], candidates=[],
        units=[WorkItem(id="work", kind="review", unit_id=run.plan.units[0].id)], excluded={})
    return store, run, lease, provider, report


def test_real_onpar_changes_survive_unrelated_and_overlapping_background(onpar_run):
    _, _, _, provider, report = onpar_run
    unrelated = provider.evidence._read("caller.py", 1, 200, "head")["evidence"]["id"]
    broad = provider.evidence._read("src/clickllm/prove/stats.py", 360, 480, "head")["evidence"]["id"]
    report.units[0].visible_source_ids = [unrelated, broad, unrelated]
    packet, refs, omissions = overview_material(report, provider)
    assert {item["path"] for item in packet["changes"]} == {
        "src/clickllm/prove/stats.py", "tests/test_stats.py",
    }
    assert any("+    lo = min(lo, point)" in item["patch"] for item in packet["changes"])
    assert any("test_the_posterior_point_estimate_lies_inside_its_own_interval" in item["patch"]
               for item in packet["changes"])
    assert len(refs) == 4
    assert {source["side"] for source in refs.values()} == {"base", "head"}
    assert not any(item.get("background_only") for item in packet["source_materials"])
    assert not omissions
    assert tokens(packet) <= MATERIAL_TOKENS
    report.units[0].visible_source_ids.reverse()
    assert overview_material(report, provider) == (packet, refs, omissions)


def test_adjacent_background_is_optional_and_does_not_duplicate_changes(onpar_run):
    _, _, _, provider, report = onpar_run
    adjacent = provider.evidence._read("src/clickllm/prove/stats.py", 430, 436, "head")["evidence"]["id"]
    same = provider.evidence._read("src/clickllm/prove/stats.py", 437, 459, "head")["evidence"]["id"]
    report.units[0].visible_source_ids = [adjacent, same, adjacent]
    packet, refs, _ = overview_material(report, provider)
    assert len(packet["changes"]) == 2
    assert len([item for item in packet["source_materials"] if item.get("background_only")]) == 1
    assert len(refs) == 5
    assert len({item["id"] for item in refs.values()}) == len(refs)


def test_units_receive_capacity_before_redistribution(tmp_path):
    from app.tools.diff_parser import DiffParser
    _, run, _, provider, _ = setup_source_run(tmp_path)
    repo = provider.evidence.repo_path
    for number in range(6):
        (repo / f"part{number}.py").write_text(f"VALUE = {number}\n", encoding="utf-8")
    base = commit(repo, "six base members")
    for number in range(6):
        (repo / f"part{number}.py").write_text(f"VALUE = {number + 100}\n", encoding="utf-8")
    head = commit(repo, "six head members")
    run.snapshot.base_sha, run.snapshot.head_sha = base, head
    run.changed_files = DiffParser().parse(git(repo, "diff", base, head))
    keys = list(members(run.changed_files))
    unit = run.plan.units[0]
    run.plan.units = [unit.model_copy(update={"id": "first", "required_hunk_ids": keys[:5]}),
                      unit.model_copy(update={"id": "last", "required_hunk_ids": keys[5:]})]
    provider.evidence.allowed_paths = frozenset(change.file_path for change in run.changed_files)
    report = Report(version="d" * 64, status="completed", findings=[], candidates=[], units=[], excluded={})
    packet, _, omissions = overview_material(report, provider, max_tokens=2400)
    assert {item["unit_id"] for item in packet["changes"]} == {"first", "last"}
    assert packet["omitted"]["changes"] > 0
    assert omissions
    assert tokens(packet) <= 2400


def test_capacity_records_exact_omissions_outside_model_context(onpar_run):
    _, _, _, provider, report = onpar_run
    packet, _, omissions = overview_material(report, provider, max_tokens=1100)
    assert tokens(packet) <= 1100
    assert packet["omitted"]["changes"] == 2
    assert len(omissions) == 4
    assert {(item.side, item.path, item.start_line, item.end_line) for item in omissions} == {
        ("base", "src/clickllm/prove/stats.py", 437, 442),
        ("head", "src/clickllm/prove/stats.py", 437, 459),
        ("base", "tests/test_stats.py", 269, 271),
        ("head", "tests/test_stats.py", 269, 305),
    }
    assert all(item.reason == "material_capacity" for item in omissions)
    assert "omitted_materials" not in packet


def test_diff_mismatch_is_not_presented_as_valid_change(onpar_run):
    _, run, _, provider, report = onpar_run
    run.changed_files[0].hunks[0].lines[0].content = "not the frozen source"
    packet, _, omissions = overview_material(report, provider)
    assert {item["path"] for item in packet["changes"]} == {"tests/test_stats.py"}
    assert {item.reason for item in omissions} == {"diff_source_mismatch"}


def test_source_policy_failure_omits_change_without_falling_back_to_background(onpar_run):
    _, _, _, provider, report = onpar_run
    background = provider.evidence._read("caller.py", 1, 20, "head")["evidence"]["id"]
    report.units[0].visible_source_ids = [background]
    provider.evidence.allowed_paths = frozenset({"caller.py"})
    packet, refs, omissions = overview_material(report, provider)
    assert packet["changes"] == []
    assert refs == {}
    assert len(omissions) == 4
    assert {item.reason for item in omissions} == {"source_unavailable"}


def test_added_deleted_and_renamed_paths_keep_correct_source_sides(tmp_path):
    from app.tools.diff_parser import DiffParser
    _, run, _, provider, _ = setup_source_run(tmp_path)
    repo = provider.evidence.repo_path
    (repo / "removed.py").write_text("REMOVED = 1\n", encoding="utf-8")
    (repo / "old.py").write_text("\n".join(f"VALUE_{i} = {i}" for i in range(12)) + "\n", encoding="utf-8")
    base = commit(repo, "before structural changes")
    (repo / "removed.py").unlink()
    (repo / "added.py").write_text("ADDED = 2\n", encoding="utf-8")
    (repo / "old.py").rename(repo / "new.py")
    with (repo / "new.py").open("a", encoding="utf-8") as handle:
        handle.write("EXTRA = 3\n")
    head = commit(repo, "after structural changes")
    run.snapshot.base_sha, run.snapshot.head_sha = base, head
    run.changed_files = DiffParser().parse(git(repo, "diff", "-M", base, head))
    run.plan = plan_review(run.changed_files, run.snapshot, run.configuration.options)
    provider.evidence.allowed_paths = frozenset({"removed.py", "added.py", "old.py", "new.py"})
    report = Report(version="d" * 64, status="completed", findings=[], candidates=[], units=[], excluded={})
    packet, refs, omissions = overview_material(report, provider)
    assert not omissions
    indexed = {change["path"]: change for change in packet["changes"]}
    assert set(indexed["added.py"]["source_refs"]) == {"head"}
    assert set(indexed["removed.py"]["source_refs"]) == {"base"}
    assert refs[indexed["new.py"]["source_refs"]["base"]]["path"] == "old.py"
    assert refs[indexed["new.py"]["source_refs"]["head"]]["path"] == "new.py"


@pytest.mark.parametrize("error", [LeaseLost("lost"), ReviewStopped("cancelled")])
def test_material_selection_propagates_cancellation_and_lease_loss(onpar_run, monkeypatch, error):
    _, _, _, provider, report = onpar_run
    def stopped(*args):
        raise error
    monkeypatch.setattr(provider.store, "assert_active", stopped)
    with pytest.raises(type(error)):
        overview_material(report, provider)


def test_hard_deadline_stops_source_reads(onpar_run, monkeypatch):
    _, run, _, provider, report = onpar_run
    run.hard_deadline = utcnow() - timedelta(seconds=1)
    def forbidden(*args):
        pytest.fail("No source read after the hard deadline")
    monkeypatch.setattr(provider.evidence, "_read", forbidden)
    with pytest.raises(BudgetExceeded, match="hard_deadline"):
        overview_material(report, provider)


async def test_policy_and_prompt_changes_invalidate_saved_overview(tmp_path, monkeypatch):
    from app.review import report as report_module
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    report = build_report(run, store.work_items(run.id), [], store.usage(run.id))
    budget = BudgetGate(store, run, lease)
    first = await generate_overview(report, provider, budget, ReviewModel())
    assert first.status == "ready"
    run.report = report
    before = store.usage(run.id)["model_calls"]
    assert await generate_overview(report, provider, budget, ReviewModel()) == first
    assert store.usage(run.id)["model_calls"] == before
    monkeypatch.setattr(report_module, "OVERVIEW_POLICY_VERSION", "next-policy")
    changed = await generate_overview(report, provider, budget, ReviewModel())
    assert changed.status == "ready" and changed.input_hash != first.input_hash
    assert store.usage(run.id)["model_calls"] == before + 1
    original_read = Path.read_text
    def read_prompt(path, *args, **kwargs):
        text = original_read(path, *args, **kwargs)
        return text + "\nKeep necessary conditions." if path.name == "overview.md" else text
    monkeypatch.setattr(Path, "read_text", read_prompt)
    newest = await generate_overview(report, provider, budget, ReviewModel())
    assert newest.status == "ready" and newest.input_hash != changed.input_hash
    assert store.usage(run.id)["model_calls"] == before + 2


async def test_real_onpar_generation_fits_original_request_protection(onpar_run):
    from sqlalchemy import select
    from app.models.orm import ReviewRequestOrm
    store, run, lease, provider, report = onpar_run
    overview = await generate_overview(report, provider, BudgetGate(store, run, lease), ReviewModel())
    assert overview.status == "ready"
    assert len(overview.sources) == 4, overview.omitted_materials
    assert overview.omitted["changes"] == 0
    from app.review.overview_materials import overview_material as material_builder
    assert material_builder(report, provider)[0]["omitted"]["changes"] == 0
    assert store.usage(run.id)["model_calls"] == 1
    with store.sessions() as session:
        request = session.scalar(select(ReviewRequestOrm).where(ReviewRequestOrm.run_id == run.id))
        assert request.reserved_input_tokens <= OVERVIEW_INPUT


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
async def test_saved_overview_keeps_paragraphs_and_code_through_publication(tmp_path, newline):
    from langchain_core.messages import AIMessage
    class ProseModel:
        def bind_tools(self, *args, **kwargs):
            return self
        async def ainvoke(self, messages):
            packet = json.loads(messages[1].content)
            handle = packet["source_materials"][0]["source_ref"]
            return AIMessage(content="", usage_metadata={"input_tokens": 100, "output_tokens": 50,
                "total_tokens": 150}, tool_calls=[{"id": "overview", "name": "OverviewOutput", "args": {
                    "change_overview": [
                        {"text": f"调整 `ratio`。{newline}{newline}保留必要边界。", "source_refs": [handle]},
                        {"text": "补充边界测试。", "source_refs": [handle]},
                    ], "finding_order": []}}])
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    report = build_report(run, store.work_items(run.id), [], store.usage(run.id))
    report.overview = await generate_overview(report, provider, BudgetGate(store, run, lease), ProseModel())
    store.save_report(run.id, report, lease)
    restored = Run.model_validate_json(store.get(run.id).model_dump_json())
    plan = PublicationPlanner(provider.evidence.artifacts).build(restored)
    assert plan.overview_status == "ready"
    assert plan.overview_markdown == "调整 `ratio`。\n\n保留必要边界。\n\n补充边界测试。"
    assert plan.overview_markdown in plan.review_comment_markdown
    assert plan.overview_markdown in plan.user_report_markdown


async def test_demo_source_is_rejected_without_extra_model_calls(tmp_path):
    from langchain_core.messages import AIMessage
    class DemoModel:
        def bind_tools(self, *args, **kwargs):
            return self
        async def ainvoke(self, messages):
            return AIMessage(content="", tool_calls=[{"id": "demo", "name": "OverviewOutput", "args": {
                "change_overview": [{"text": "不属于本轮的示例。", "source_refs": ["demo:range-impl"]}],
                "finding_order": []}}])
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    report = build_report(run, store.work_items(run.id), [], store.usage(run.id))
    overview = await generate_overview(report, provider, BudgetGate(store, run, lease), DemoModel())
    assert overview.status == "failed"
    assert overview.output is None
    assert store.usage(run.id)["model_calls"] == 1


async def test_empty_plan_does_not_read_or_invoke_model(tmp_path):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    run.plan.units = []
    report = build_report(run, [], [], store.usage(run.id))
    overview = await generate_overview(report, provider, BudgetGate(store, run, lease), ReviewModel())
    assert overview.status == "skipped_empty"
    assert store.usage(run.id)["model_calls"] == 0
