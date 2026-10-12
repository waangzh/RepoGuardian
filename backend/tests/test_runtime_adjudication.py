from copy import deepcopy

import pytest

from app.evaluation.runtime_adjudication import adjudicate_annotations


HEAD = "a" * 40
BASE = "b" * 40


def packets():
    return [{"blind_id": identity, "snapshot": {
        "repository": "org/repo", "head_sha": HEAD, "base_sha": BASE,
    }} for identity in ("p1", "p2", "p3")]


def annotation(identity="p1"):
    return {"blind_id": identity, "label": "valid_defect", "reason": "已核对冻结源码",
            "code_evidence": [{"path": "src/a.py", "side": "head", "snapshot_sha": HEAD,
                               "start_line": 2, "end_line": 2, "code": "return None"}]}


def adjudicate(items, *, blind=True, reader=None):
    return adjudicate_annotations(packets(), items, reviewer_id="independent-reviewer", blind=blind,
                                  source_reader=reader or (lambda *args: "def f():\nreturn None\n"))


def test_unrelated_rejected_annotation_preserves_valid_item_and_missing_stays_unreviewed():
    invalid = annotation("p2")
    invalid["code_evidence"][0]["code"] = "return validated"
    report = adjudicate([annotation(), invalid])
    assert [item["blind_id"] for item in report["accepted"]] == ["p1"]
    assert report["rejected"][0]["reason"] == "evidence_text_mismatch"
    assert report["quality"]["not_reviewed"] == 2
    assert report["adjudication_status"] == "pending"
    assert report["quality"]["precision"] is None


@pytest.mark.parametrize("change,reason", [
    ({"path": "../private.py"}, "evidence_path_rejected"),
    ({"path": "src/../private.py"}, "evidence_path_rejected"),
    ({"path": "D:/outside.py"}, "evidence_path_rejected"),
    ({"path": "src\\a.py"}, "evidence_path_rejected"),
    ({"path": ".env"}, "evidence_path_rejected"),
    ({"snapshot_sha": "c" * 40}, "evidence_snapshot_mismatch"),
    ({"side": []}, "evidence_snapshot_mismatch"),
    ({"start_line": True}, "evidence_range_rejected"),
    ({"end_line": 1}, "evidence_range_rejected"),
])
def test_illegal_source_identity_is_rejected_before_reading(change, reason):
    item = annotation()
    item["code_evidence"][0].update(change)
    reads = []
    report = adjudicate([item], reader=lambda *args: reads.append(args))
    assert not reads and not report["accepted"]
    assert report["rejected"][0]["reason"] == reason


def test_missing_source_and_out_of_bounds_do_not_accept_quality_labels():
    report = adjudicate([annotation()], reader=lambda *args: None)
    assert report["rejected"][0]["reason"] == "evidence_source_unavailable"
    report = adjudicate([annotation()], reader=lambda *args: "def f():")
    assert report["rejected"][0]["reason"] == "evidence_text_mismatch"


def test_duplicate_annotation_collision_rejects_both_instead_of_last_writer_wins():
    other = deepcopy(annotation())
    other["label"] = "false_positive"
    report = adjudicate([annotation(), other])
    assert not report["accepted"] and len(report["rejected"]) == 2
    assert {item["reason"] for item in report["rejected"]} == {"conflicting_quality_identity"}


def test_duplicate_binding_is_invalidated_when_its_source_label_is_rejected():
    original = annotation()
    original["code_evidence"][0]["snapshot_sha"] = BASE
    duplicate = {"blind_id": "p2", "label": "duplicate", "duplicate_of": "p1",
                 "reason": "同一失败条件"}
    report = adjudicate([original, duplicate])
    assert not report["accepted"] and len(report["rejected"]) == 2
    assert report["rejected"][1]["reason"] == "duplicate_requires_accepted_valid_defect"


def test_cannot_determine_is_an_answer_but_nonblind_labels_cannot_close_blind_gate():
    items = [{"blind_id": identity, "label": "cannot_determine", "reason": "外部契约不可得"}
             for identity in ("p1", "p2", "p3")]
    report = adjudicate(items)
    assert report["quality"]["cannot_determine"] == 3
    assert report["adjudication_status"] == "complete"
    assert adjudicate(items, blind=False)["adjudication_status"] == "pending"
    assert report["recall_evaluation"] == "not_performed"


def test_malformed_sibling_is_rejected_without_losing_valid_annotation():
    report = adjudicate([[], {"blind_id": []}, {"blind_id": "p2", "label": []}, annotation()])
    assert len(report["rejected"]) == 3
    assert [item["blind_id"] for item in report["accepted"]] == ["p1"]


def test_source_reader_rejects_external_roots_and_symlink_objects_before_blob_read(tmp_path):
    from app.evaluation.runtime_adjudication_cli import make_source_reader

    boundary = tmp_path / "runs"
    repository = boundary / "source"
    repository.mkdir(parents=True)
    source = {"repository": "org/repo", "source_root": str(repository),
              "head_sha": HEAD, "base_sha": BASE}
    reads = []

    class GitStub:
        def get_tree_entries(self, root, sha, paths):
            return {paths[0]: {"mode": "120000"}}

        def get_file_content_at_revision(self, *args):
            reads.append(args)
            return "private source"

    reader = make_source_reader([source], boundary, git_tool=GitStub())
    assert reader("org/repo", HEAD, "src/link.py") is None
    assert not reads
    assert reader("org/repo", "c" * 40, "src/a.py") is None
    assert reader("other/repo", HEAD, "src/a.py") is None
    external = {**source, "source_root": str(tmp_path)}
    with pytest.raises(ValueError, match="source_root_identity_or_boundary_rejected"):
        make_source_reader([external], boundary, git_tool=GitStub())


def test_source_reader_reads_exact_registered_sha_and_caches_bounded_blob_identity(tmp_path):
    from app.evaluation.runtime_adjudication_cli import make_source_reader

    root = tmp_path / "source"
    root.mkdir()
    source = {"repository": "org/repo", "source_root": str(root),
              "head_sha": HEAD, "base_sha": BASE}
    reads = []

    class GitStub:
        def get_tree_entries(self, root, sha, paths):
            return {paths[0]: {"mode": "100644"}}

        def get_file_content_at_revision(self, root, sha, path):
            reads.append((sha, path))
            return "head source" if sha == HEAD else "base source"

    reader = make_source_reader([source], tmp_path, git_tool=GitStub())
    assert reader("org/repo", HEAD, "src/a.py") == "head source"
    assert reader("org/repo", HEAD, "src/a.py") == "head source"
    assert reader("org/repo", BASE, "src/a.py") == "base source"
    assert reads == [(HEAD, "src/a.py"), (BASE, "src/a.py")]
