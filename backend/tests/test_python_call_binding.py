import pytest

from app.languages.adapters import TreeSitterLanguageAdapter, load_python_grammar
from app.tools.repository_graph import build_repository_graph


def indexed_graph(tmp_path, source, *, caller="caller.py", target="src/pkg/stats.py", extra=None,
                  target_source="def measure(values):\n    return values\n", version="import-evidence-v4"):
    files, symbols = [], []
    adapter = TreeSitterLanguageAdapter("python", {".py"}, load_python_grammar)
    for path, content in {caller: source, target: target_source, **(extra or {})}.items():
        file = tmp_path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding="utf-8")
        analysis = adapter.analyze(file, path)
        assert not analysis.diagnostics
        files.append({"path": path, "language": "python",
                      "import_refs": [ref.model_dump(mode="json") for ref in analysis.imports]})
        symbols.extend(symbol.to_index_entry() for symbol in analysis.symbols)
    return build_repository_graph(files, symbols, call_resolution_version=version), symbols


@pytest.mark.parametrize("statement,expression", [
    ("from pkg.stats import measure", "measure"),
    ("from pkg.stats import measure as check", "check"),
    ("import pkg.stats", "pkg.stats.measure"),
    ("import pkg.stats as stats", "stats.measure"),
    ("from .stats import measure", "measure"),
])
def test_explicit_imports_resolve_even_with_same_name_elsewhere(tmp_path, statement, expression):
    graph, symbols = indexed_graph(tmp_path, f"{statement}\n\ndef run():\n    return {expression}([])\n",
        caller="src/pkg/caller.py", extra={"unrelated.py": "def measure(values):\n    return None\n"})
    edges = [edge for edge in graph["edges"] if edge["type"] == "calls"]
    assert len(edges) == 1
    assert edges[0]["target"] == "src/pkg/stats.py::measure"
    assert edges[0]["resolution_status"] == "resolved"
    assert edges[0]["resolution_basis"] == "python_lexical_import"
    assert edges[0]["call_sites"][0]["line"] == 4
    assert edges[0]["call_sites"][0]["import_binding"]["scope_start_line"] == 1
    assert any(symbol["python_binding_safe"] for symbol in symbols)


@pytest.mark.parametrize("source", [
    "from pkg.stats import measure\ndef run(measure):\n    return measure([])\n",
    "from pkg.stats import measure\ndef run():\n    measure = replacement\n    return measure([])\n",
    "from pkg.stats import measure\nmeasure = replacement\ndef run():\n    return measure([])\n",
    "if enabled:\n    from pkg.stats import measure\ndef run():\n    return measure([])\n",
    "from pkg.stats import *\ndef run():\n    return measure([])\n",
    "from pkg.stats import measure\ndef run():\n    exec(code)\n    return measure([])\n",
    "from pkg.stats import measure\ndef mutate():\n    global measure\n    measure = replacement\ndef run():\n    return measure([])\n",
    "import pkg.stats as stats\ndef run():\n    stats.measure = replacement\n    return stats.measure([])\n",
    "def one():\n    from pkg.stats import measure\ndef two():\n    return measure([])\n",
    "from pkg.stats import measure\ndef run(values):\n    return [measure([]) for measure in values]\n",
    "from pkg.stats import measure\ndef run(value):\n    match value:\n        case {'a': other, **measure}:\n            return measure([])\n",
    "def outer():\n    from pkg.stats import measure\n    def mutate():\n        nonlocal measure\n        measure = replacement\n    def run():\n        return measure([])\n    return run\n",
])
def test_shadowed_conditional_or_dynamic_binding_is_never_resolved(tmp_path, source):
    graph, _ = indexed_graph(tmp_path, source)
    assert not [edge for edge in graph["edges"] if edge["type"] == "calls"
                and edge.get("resolution_status") == "resolved"]


def test_local_import_and_nested_calls_have_actual_scope_and_all_call_sites(tmp_path):
    source = ("def outer():\n    def inner():\n        from pkg.stats import measure\n"
              "        measure([])\n        return measure([1])\n    return inner()\n")
    graph, _ = indexed_graph(tmp_path, source)
    edges = [edge for edge in graph["edges"] if edge["type"] == "calls"
             and edge.get("resolution_status") == "resolved"]
    assert len(edges) == 1 and edges[0]["source"] == "caller.py::inner"
    assert [site["line"] for site in edges[0]["call_sites"]] == [4, 5]


@pytest.mark.parametrize("target_source", [
    "def measure(values):\n    return values\nmeasure = replacement\n",
    "@decorate\ndef measure(values):\n    return values\n",
    "if enabled:\n    def measure(values):\n        return values\n",
    "def measure(values):\n    return values\ndef measure(values):\n    return None\n",
])
def test_unstable_target_definition_is_not_proven(tmp_path, target_source):
    graph, _ = indexed_graph(tmp_path, "from pkg.stats import measure\ndef run():\n    return measure([])\n",
                              target_source=target_source)
    assert not [edge for edge in graph["edges"] if edge.get("resolution_status") == "resolved"]


def test_legacy_resolution_remains_frozen(tmp_path):
    graph, _ = indexed_graph(tmp_path, "from pkg.stats import measure\ndef run():\n    return measure([])\n",
                              version="import-evidence-v3")
    edge = next(edge for edge in graph["edges"] if edge["type"] == "calls")
    assert edge["resolution_status"] == "unresolved" and edge["resolution_basis"] == "unique_name"
