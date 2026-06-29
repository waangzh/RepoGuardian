from app.tools.diff_parser import DiffParser


def test_parse_modified_file_diff() -> None:
    diff = """diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1,2 +1,3 @@
 def hello():
-    return "hi"
+    name = "RepoGuardian"
+    return f"hi {name}"
"""
    files = DiffParser().parse(diff)

    assert len(files) == 1
    assert files[0].file_path == "app.py"
    assert files[0].change_type == "modified"
    assert files[0].additions == 2
    assert files[0].deletions == 1
    assert files[0].hunks[0].added_lines[0].line_no == 2


def test_parse_added_file_diff() -> None:
    diff = """diff --git a/new.py b/new.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/new.py
@@ -0,0 +1 @@
+print("new")
"""
    files = DiffParser().parse(diff)

    assert files[0].file_path == "new.py"
    assert files[0].change_type == "added"
    assert files[0].additions == 1

