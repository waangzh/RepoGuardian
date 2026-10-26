import pytest

from app.languages.base import AnalysisLevel, SymbolRef
from app.languages.registry import default_language_registry
from app.tools.repo_indexer import RepoIndexer


SOURCES = {
    "go": ("main.go", '''package example
import (
    f "fmt"
    . `os`
    _ "net/http"
)
type Box[T any] struct { value T }
type Reader interface { Read(x int) error }
type Alias = Box[int]
const (
    First, Second = 1, 2
    Third = 3
)
func (b *Box[T]) Get(
    value T,
) T {
    f.Println(value)
    closure := func() { hidden() }
    _ = closure
    return value
}
func Echo[T any](x T) T { return x }
func 使用() { Echo[int](1) }
// func phantom() {}
var text = "func fake() {}"
'''),
    "java": ("Outer.java", '''import static java.util.Collections.*;
import java.util.List;
@interface Marker { String value() default "ok"; }
public class Outer {
    static final int FIRST = 1, SECOND = 2;
    interface Reader { void read(); }
    enum State { READY }
    class Inner {
        Inner() { check(); }
        <T> T get(
            T value
        ) { return convert(value); }
        void run() { target.work(); new Target(); Runnable r = () -> hidden();
            Object a = new Target() { void invisible() { nested(); } };
        }
        void same(int a) {} void same(String b) {}
        void 使用() { run(); }
    }
    // void phantom() {}
    String text = "void fake() {}";
}
record Result(int value) { Result { check(); } }
'''),
    "rust": ("lib.rs", '''use crate::models::{self, User as Person, nested::{Item, *}};
mod external;
pub mod inner {
    pub struct Box<T>(T);
    enum State { Ready }
    pub trait Reader { fn read(&self); type Output; }
    impl<T> Box<T> {
        pub fn get<U>(
            &self, value: U,
        ) -> U where U: Clone {
            helper::convert::<U>(value.clone());
            let closure = || hidden();
            fn nested() { separate(); }
            println!("opaque {}", inside_macro());
            value
        }
    }
    impl Reader for Box<i32> { fn read(&self) {} }
    pub const LIMIT: i32 = 1;
    static COUNT: i32 = 0;
    type Alias = i32;
    pub fn 使用() {}
    macro_rules! generated { () => { fn phantom() {} }; }
    const TEXT: &str = "fn fake() {}";
}
'''),
}


@pytest.fixture(params=SOURCES)
def analysis(request, tmp_path):
    language = request.param
    filename, source = SOURCES[language]
    path = tmp_path / filename
    path.write_text(source, encoding="utf-8")
    result = default_language_registry.analyze(path, filename)
    assert result.level == AnalysisLevel.semantic, result.diagnostics
    assert result.parser_id == f"tree-sitter.{language}.v1"
    assert result.diagnostics == []
    assert len({symbol.symbol_id for symbol in result.symbols}) == len(result.symbols)
    assert all(symbol.confidence >= 0.9 and symbol.symbol_id for symbol in result.symbols)
    assert not any("phantom" in symbol.name or "fake" in symbol.name for symbol in result.symbols)
    return language, source, result


def test_real_grammars_declarations_calls_and_imports(analysis):
    language, source, result = analysis
    symbols = {symbol.name: symbol for symbol in result.symbols}
    expected = {
        "go": {"Box", "Reader", "Reader.Read", "Alias", "First", "Second", "Third", "Box.Get", "Echo", "使用"},
        "java": {"Marker", "Marker.value", "Outer", "Outer.Reader", "Outer.Reader.read", "Outer.State",
                 "Outer.FIRST", "Outer.SECOND", "Outer.Inner", "Outer.Inner.Inner", "Outer.Inner.get",
                 "Outer.Inner.run", "Outer.Inner.same", "Outer.Inner.使用", "Result", "Result.Result"},
        "rust": {"external", "inner", "inner.Box", "inner.State", "inner.Reader", "inner.Reader.read",
                 "inner.Reader.Output", "inner.Box.get", "inner.Box.get.nested", "inner.Box.read",
                 "inner.LIMIT", "inner.COUNT", "inner.Alias", "inner.使用", "inner.TEXT"},
    }[language]
    assert set(symbols) == expected
    target = symbols[{"go": "Box.Get", "java": "Outer.Inner.get", "rust": "inner.Box.get"}[language]]
    assert "\n" in target.signature
    assert target.container == {"go": "Box", "java": "Outer.Inner", "rust": "inner.Box"}[language]
    assert target.kind == "method"
    assert target.end_line > target.start_line
    start_text = {"go": "func (b *Box[T]) Get(", "java": "        <T> T get(",
                  "rust": "        pub fn get<U>("}[language]
    end_text = {"go": "}", "java": "        ) { return convert(value); }",
                "rust": "        }"}[language]
    assert target.start_line == source.splitlines().index(start_text) + 1
    assert target.end_line == source.splitlines().index(end_text, target.start_line) + 1
    assert "hidden" not in {call.simple_name for symbol in result.symbols for call in symbol.calls}
    assert "inside_macro" not in {call.simple_name for symbol in result.symbols for call in symbol.calls}
    for symbol in result.symbols:
        assert 1 <= symbol.start_line <= symbol.end_line <= len(source.splitlines())
        assert symbol.to_index_entry()["symbol_id"] == symbol.symbol_id
        for call in symbol.calls:
            assert symbol.start_line <= call.line <= symbol.end_line
            assert call.scope_start_line == symbol.start_line
            assert call.simple_name in source.splitlines()[call.line - 1]
    if language == "go":
        assert {ref.module: ref.local_name for ref in result.imports} == {"fmt": "f", "os": ".", "net/http": "_"}
        assert [call.callee for call in target.calls] == ["f.Println"]
        assert target.exported is True
        generic = symbols["使用"].calls[0]
        assert generic.callee == "Echo[int]" and generic.simple_name == "Echo"
        assert generic.binding_reason == "syntax_ambiguous_type_or_call"
        assert generic.confidence < 0.9
    elif language == "java":
        assert {ref.module for ref in result.imports} == {"java.util.Collections.*", "java.util.List"}
        assert next(ref for ref in result.imports if ref.module.endswith(".*")).kind == "static_import"
        overloads = [symbol for symbol in result.symbols if symbol.name == "Outer.Inner.same"]
        assert len(overloads) == 2 and overloads[0].start_line == overloads[1].start_line
        assert {call.callee for call in symbols["Outer.Inner.run"].calls} == {"target.work", "Target"}
        assert {call.callee for call in symbols["Result.Result"].calls} == {"check"}
    else:
        assert {ref.module for ref in result.imports} == {
            "crate::models", "crate::models::User", "crate::models::nested::Item",
            "crate::models::nested::*", "external"}
        assert next(ref for ref in result.imports if ref.local_name == "Person").imported_name == "User"
        assert {call.simple_name for call in target.calls} == {"convert", "clone"}
        assert symbols["inner.Reader.read"].kind == "method"
        assert [call.callee for call in symbols["inner.Box.get.nested"].calls] == ["separate"]


@pytest.mark.parametrize("language", SOURCES)
def test_empty_and_unreadable_sources(language, tmp_path):
    filename, _ = SOURCES[language]
    path = tmp_path / filename
    path.write_text("", encoding="utf-8")
    result = default_language_registry.analyze(path, filename)
    assert result.level == AnalysisLevel.semantic and not result.symbols
    result = default_language_registry.analyze(tmp_path / "missing" / filename, filename)
    assert result.level == AnalysisLevel.text and result.diagnostics


@pytest.mark.parametrize("language", SOURCES)
@pytest.mark.parametrize("failure", ["grammar", "syntax"])
def test_failure_is_explicit_l1(language, failure, tmp_path, monkeypatch):
    filename, source = SOURCES[language]
    path = tmp_path / filename
    path.write_text(source if failure == "grammar" else "@@@ invalid {{{", encoding="utf-8")
    if failure == "grammar":
        def unavailable(_):
            raise ImportError("grammar unavailable")
        adapter = default_language_registry.get(language)
        monkeypatch.setattr(adapter, "_grammar_loader", unavailable)
        result = adapter._analyze_native(path.read_bytes(), filename, path.suffix)
    else:
        result = default_language_registry.analyze(path, filename)
    assert result.level == AnalysisLevel.heuristic
    assert result.parser_id == f"regex.{language}.v1"
    assert "downgraded to L1" in result.diagnostics[0]
    assert all(symbol.confidence < 0.9 and symbol.symbol_id is None for symbol in result.symbols)


def test_optional_symbol_identity_is_omitted():
    symbol = SymbolRef(file="a.py", name="f", kind="function", start_line=1, end_line=1,
                       confidence=0.98, parser_id="tree-sitter.python.v1")
    assert "symbol_id" not in symbol.to_index_entry()








@pytest.mark.asyncio
async def test_overloads_do_not_collapse_into_edges(tmp_path):
    (tmp_path / "Over.java").write_text(
        "class Over { void read(int a) {} void read(String a) {} void run() { read(1); } }",
        encoding="utf-8")
    index = await RepoIndexer().execute(repo_path=str(tmp_path))
    assert len(index["repository_graph"]["symbols"]) == 4
    assert not [edge for edge in index["repository_graph"]["edges"] if edge["type"] == "calls"]


@pytest.mark.asyncio
async def test_downgrade_diagnostic_reaches_file_index(tmp_path):
    (tmp_path / "broken.go").write_text("package p\nfunc Broken( {", encoding="utf-8")
    index = await RepoIndexer().build_file_index(str(tmp_path))
    assert index[0]["analysis_level"] == 1
    assert index[0]["parser_id"] == "regex.go.v1"
    assert index[0]["diagnostics"]






def test_go_grouped_types_aliases_and_source_order(tmp_path):
    source = """package p
type (
    Thing struct {}
    Reader interface { Read() }
    Alias = struct {}
)
"""
    path = tmp_path / "types.go"
    path.write_text(source, encoding="utf-8")
    result = default_language_registry.analyze(path, path.name)
    assert result.level == AnalysisLevel.semantic, result.diagnostics
    assert [(symbol.name, symbol.kind) for symbol in result.symbols] == [
        ("Thing", "struct"), ("Reader", "interface"), ("Reader.Read", "method"), ("Alias", "type")]


def test_rust_trait_impl_collisions_and_raw_identifiers(tmp_path):
    path = tmp_path / "impls.rs"
    path.write_text("""mod a {
    struct S;
    trait T { fn same(); }
    trait U { fn same(); }
    impl T for S { fn same() { a::r#match::<u8>(); } }
    impl U for S { fn same() {} }
}
""", encoding="utf-8")
    result = default_language_registry.analyze(path, path.name)
    assert result.level == AnalysisLevel.semantic, result.diagnostics
    methods = [symbol for symbol in result.symbols if symbol.name == "a.S.same"]
    assert len(methods) == 2 and methods[0].symbol_id != methods[1].symbol_id
    assert methods[0].calls[0].callee == "a::r#match::<u8>"
    assert methods[0].calls[0].simple_name == "match"


@pytest.mark.asyncio
@pytest.mark.parametrize("language,filename,source,expected", [
    ("java", "Types.java", "interface I {} enum E { A } record R(int x) {} @interface Ann {}", {"I", "E", "R", "Ann"}),
    ("rust", "types.rs", "struct S; enum E { A } trait T {} type A = i32;", {"S", "E", "T", "A"}),
])
async def test_compiled_type_definition_kinds(language, filename, source, expected, tmp_path):
    (tmp_path / filename).write_text(source, encoding="utf-8")
    index = await RepoIndexer().execute(repo_path=str(tmp_path))
    assert index["file_index"][0]["analysis_level"] == 2
    assert index["file_index"][0]["language"] == language
    declarations = {symbol["symbol"] for symbol in index["symbol_index"]
                    if symbol["type"] in {"interface", "enum", "record", "annotation", "struct", "trait", "type"}}
    assert expected <= declarations
