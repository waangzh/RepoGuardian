import { marked } from 'marked';

export interface CommentPreviewSection {
  title: string;
  markdown: string;
  findings: boolean;
}

/** Split only the known delivery document; unknown or historical formats keep their full Markdown. */
export function parseCommentPreview(markdown: string) {
  const tokens = marked.lexer(markdown);
  const meaningful = tokens.filter(token => token.type !== 'space');
  const first = meaningful[0];
  const title = first?.type === 'paragraph' || first?.type === 'heading' ? first.text : '';
  const verdict = meaningful[1];
  const match = verdict?.type === 'paragraph'
    ? /^(✅|⚠️?|❓)\s*(结论[：:].+)$/u.exec(verdict.text.trim()) : null;
  if (title !== 'RepoGuardian · PR Code Review' || !match) return null;

  const sections: CommentPreviewSection[] = [];
  let section: CommentPreviewSection | null = null;
  let snapshot = '';
  let inSnapshot = false;
  const start = tokens.indexOf(verdict!) + 1;
  for (let index = start; index < tokens.length; index += 1) {
    const token = tokens[index]!;
    if (token.type === 'hr' && /^\s*本结论基于/.test(tokens.slice(index + 1).map(item => item.raw).join(''))) {
      inSnapshot = true;
      continue;
    }
    if (inSnapshot) { snapshot += token.raw; continue; }
    if (token.type === 'heading' && token.depth === 2) {
      const label = ({ '发现的问题': '已确认问题', '其他审查结果': '审查局限' } as Record<string, string>)[token.text] || token.text;
      section = { title: label, markdown: '', findings: label === '已确认问题' };
      sections.push(section);
    } else if (section) {
      // Older comments used this label for the same source-bound observation.
      const raw = section.title === '变更概述' && token.type === 'paragraph'
        ? token.raw.replace(/^\*\*实际变更[：:]\*\*/, '**代码观察：**') : token.raw;
      section.markdown += raw;
    } else if (token.type !== 'space') {
      return null; // Never silently drop an unfamiliar preamble.
    }
  }
  if (!sections.length) return null;
  return { title, conclusion: match[2]!, positive: match[1] === '✅', sections, snapshot: snapshot.trim() };
}
