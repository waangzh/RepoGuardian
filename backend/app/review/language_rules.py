"""按仓库相对路径匹配语言，并生成有界的审查规则上下文。"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Iterable


_EXTENSION_LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
}

_MARKDOWN_LANGUAGES = {
    "python": "python",
    "javascript": "javascript",
    "typescript": "typescript",
    "java": "java",
    "go": "go",
    "rust": "rust",
}

_COMMON_JAVASCRIPT_RULES = (
    "检查 Promise/async 错误传播、遗漏 await 和竞态条件。",
    "区分 null、undefined、假值和缺失字段，避免错误的真值判断。",
    "检查模块导入导出、运行时环境与副作用是否兼容。",
)

_LANGUAGE_RULES: dict[str, tuple[str, ...]] = {
    "python": (
        "检查可变默认参数、异常边界、上下文管理器和异步调用是否正确。",
        "检查 None、动态类型与导入副作用造成的运行时失败。",
    ),
    "javascript": _COMMON_JAVASCRIPT_RULES,
    "typescript": (
        *_COMMON_JAVASCRIPT_RULES,
        "不要把类型检查当作运行时校验；检查不安全断言、类型收窄和 any 泄漏。",
        "检查可选属性、联合类型与穷尽分支在 undefined 输入下的行为。",
    ),
    "java": (
        "检查 null、异常传播、资源关闭、并发可见性和集合可变性。",
        "检查 equals/hashCode、泛型擦除和框架生命周期约束。",
    ),
    "go": (
        "检查 error 是否被处理、defer 时机、goroutine 生命周期和共享状态竞态。",
        "检查 nil 接口、切片/映射别名和 context 取消是否正确传播。",
    ),
    "rust": (
        "检查 Result/Option 错误路径、panic 边界和 unsafe 不变量。",
        "检查所有权转换、生命周期、并发共享与整数溢出语义。",
    ),
}


def language_for_path(
    path: str,
    file_index: Iterable[dict[str, Any]] | None = None,
) -> str:
    """优先使用索引语言，缺失时按扩展名确定性回退。"""
    normalized = PurePosixPath(path).as_posix()
    for item in file_index or ():
        if str(item.get("path", "")) == normalized:
            language = str(item.get("language", "unknown"))
            if language != "unknown":
                return language
            break
    return _EXTENSION_LANGUAGES.get(PurePosixPath(normalized).suffix.casefold(), "unknown")


def markdown_language_for_path(
    path: str,
    file_index: Iterable[dict[str, Any]] | None = None,
) -> str:
    """返回安全的 Markdown fence 语言；未知文件使用 text。"""
    return _MARKDOWN_LANGUAGES.get(language_for_path(path, file_index), "text")


def build_language_context(
    paths: Iterable[str],
    file_index: Iterable[dict[str, Any]] | None = None,
    project_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """为当前有界文件集合生成按语言合并的规则包。"""
    index = list(file_index or ())
    files_by_language: dict[str, list[str]] = {}
    for path in dict.fromkeys(str(item) for item in paths if item):
        language = language_for_path(path, index)
        if language in _LANGUAGE_RULES:
            files_by_language.setdefault(language, []).append(path)

    metadata = project_meta or {}
    languages = list(files_by_language)
    return {
        "primary_language": languages[0] if languages else metadata.get("language", "unknown"),
        "languages": languages,
        "repository_languages": list(metadata.get("languages") or []),
        "framework": metadata.get("framework"),
        "is_mixed_language_repository": bool(metadata.get("is_mixed_language", False)),
        "rule_packs": [
            {
                "id": f"review.language.{language}",
                "language": language,
                "matched_files": files_by_language[language],
                "rules": list(_LANGUAGE_RULES[language]),
            }
            for language in languages
        ],
    }


def render_language_rule_context(context: dict[str, Any]) -> str:
    """将规则上下文渲染为紧凑、可直接注入 Prompt 的文本。"""
    packs = context.get("rule_packs") or []
    if not packs:
        return ""
    lines = ["## Applicable language review rules"]
    repository_languages = context.get("repository_languages") or []
    if repository_languages:
        lines.append(
            "Repository languages: " + ", ".join(str(item) for item in repository_languages)
        )
    framework = context.get("framework")
    if framework:
        lines.append(f"Detected framework: {framework}")
    lines.append(
        "Apply only rules matched to the listed files. Treat them as review guidance, "
        "not evidence that an issue exists."
    )
    for pack in packs:
        lines.append(
            f"### {pack['id']} | files: {', '.join(pack.get('matched_files') or [])}"
        )
        lines.extend(f"- {rule}" for rule in pack.get("rules") or [])
    return "\n".join(lines)
