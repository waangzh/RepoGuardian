import { marked, type Token } from "marked";
import type { ReviewTask } from "../types/review";

type SummaryTask = Pick<ReviewTask, "engine" | "pr" | "changed_files" | "review_units" | "review_unit_results" | "report_markdown">;
export interface PrChangeSummary {
  text: string;
  source: "report" | "review" | "author" | "title" | "files";
  subtitle: string;
  languages: string[];
}

const conventionalTitle = /^(build|chore|ci|docs|feat|fix|perf|refactor|revert|style|test)(?:\(([^)]+)\))?!?:\s*/i;
const languageNames: Record<string, string> = {
  py: "Python", pyi: "Python", ts: "TypeScript", tsx: "TypeScript", js: "JavaScript", jsx: "JavaScript",
  mjs: "JavaScript", cjs: "JavaScript", vue: "Vue", svelte: "Svelte", java: "Java", go: "Go", rs: "Rust",
  rb: "Ruby", php: "PHP", cs: "C#", c: "C", h: "C", cpp: "C++", cc: "C++", hpp: "C++", kt: "Kotlin", swift: "Swift",
};
const chinese = (value: string) => /[\u4e00-\u9fff]/.test(value);
const concise = (value: string) => {
  const text = value.replace(/\s+/g, " ").trim();
  const sentences = text.match(/[^。！？]+[。！？]?/g) || [text];
  const characters = Array.from(sentences.slice(0, 2).join(""));
  return characters.length > 140 ? `${characters.slice(0, 139).join("")}…` : characters.join("");
};

function inlineText(tokens: Token[] = []): string {
  return tokens.map(token => {
    if (token.type === "html" || token.type === "image") return "";
    if (token.type === "br") return " ";
    if ("tokens" in token && Array.isArray(token.tokens)) return inlineText(token.tokens);
    return "text" in token && typeof token.text === "string" ? token.text : "";
  }).join("");
}

function paragraphs(markdown: string): string[] {
  return marked.lexer(markdown.replace(/<!--[\s\S]*?-->/g, "")).flatMap(token =>
    token.type === "paragraph" ? [inlineText(token.tokens).trim()] : []);
}

function reportPurpose(markdown: string): string | null {
  let inPurpose = false;
  for (const token of marked.lexer(markdown)) {
    if (token.type === "heading" && token.depth <= 2) {
      inPurpose = /^(?:PR\s*作用|变更摘要|本次\s*PR\s*做了什么)$/.test(token.text.trim());
    } else if (inPurpose && token.type === "paragraph") {
      const text = inlineText(token.tokens).replace(/^(?:作者意图(?:（中文概括）)?|作用概括(?:（根据标题）)?|标题概括)[：:]\s*/, "").trim();
      if (chinese(text) && !/^(?:未获得|尚未|未提供|实际改动[：:])/.test(text)) return concise(text);
    }
  }
  return null;
}

export function prChangeSummary(task: SummaryTask, report?: string | null): PrChangeSummary {
  const languages = [...new Set(task.changed_files.filter(file => !file.is_binary)
    .map(file => languageNames[file.file_path.split(".").pop()?.toLowerCase() || ""]).filter(Boolean))];
  const result = (text: string, source: PrChangeSummary["source"], subtitle: string): PrChangeSummary => ({ text, source, subtitle, languages });
  const purpose = reportPurpose(report || task.report_markdown || "");
  if (purpose) return result(purpose, "report", "来自审查报告的中文变更摘要");
  if (task.engine !== "task_review_v1") {
    const summaries = [...new Set(task.review_unit_results.map(item => item.plan?.change_summary?.trim())
      .filter((value): value is string => Boolean(value && chinese(value))))];
    if (summaries.length) return result(concise(summaries.join("；")), "review", "来自审查分组的变更摘要");
  }
  const title = task.pr?.title.trim() || "";
  const cleanTitle = title.replace(conventionalTitle, "");
  if (chinese(cleanTitle)) return result(concise(cleanTitle), "title", "根据 PR 标题概括变更目的");
  const author = paragraphs(task.pr?.body || "")[0];
  if (author && chinese(author)) return result(concise(author), "author", "摘自作者提供的变更说明");

  const tests = task.changed_files.filter(file => /(?:^|\/)(?:__tests__|tests?|spec)\/|(?:^|\/)test_[^/]+$|[._](?:test|spec)\.[^/]+$/i.test(file.file_path));
  const hasImplementation = tests.length < task.changed_files.length;
  const match = title.match(conventionalTitle);
  const subject = match?.[2]?.trim() || (languages.length === 1 ? `${languages[0]} 代码` : "代码");
  const actions: Record<string, string> = {
    fix: `修复 ${subject} 相关问题`, feat: `新增 ${subject} 相关功能`, refactor: `重构 ${subject} 相关实现`,
    perf: `优化 ${subject} 的性能`, test: "调整测试用例", docs: "更新项目文档", build: "调整构建配置",
    ci: "调整持续集成配置", style: `调整 ${subject} 的代码格式`, chore: "更新项目维护内容", revert: "回退先前的变更",
  };
  if (match) {
    const action = actions[match[1].toLowerCase()];
    return result(`${action}${tests.length && hasImplementation && match[1].toLowerCase() !== "test" ? "，并更新相关测试" : ""}。`, "title", "根据 PR 标题与变更文件概括");
  }
  const text = !task.changed_files.length ? "尚无可概括的变更记录。"
    : !hasImplementation ? "本次变更调整测试文件。"
    : `本次变更调整${languages.length ? ` ${languages.join(" / ")} ` : ""}文件${tests.length ? "，并同步修改相关测试" : ""}。`;
  return result(text, "files", "根据实际变更文件概括，尚无中文作用摘要");
}
