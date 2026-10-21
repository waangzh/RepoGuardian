import { marked } from 'marked';

export interface CommentPreviewSection {
  title: string;
  markdown: string;
  findings: boolean;
  findingCount?: number;
}

/** Split only the known delivery document; unknown or historical formats keep their full Markdown. */
export function parseCommentPreview(markdown: string) {
  const tokens = marked.lexer(markdown);
  const meaningful = tokens.filter(token => token.type !== 'space');
  const first = meaningful[0];
  const title = first?.type === 'paragraph' || first?.type === 'heading' ? first.text : '';
  const verdict = meaningful[1];
  const match = verdict?.type === 'paragraph'
    ? /^(✅|⚠️?|❓|❌|⏹)\s*(结论[：:].+)$/u.exec(verdict.text.trim()) : null;
  if (title !== 'RepoGuardian · PR Code Review' || !match) return null;

  const sections: CommentPreviewSection[] = [];
  let section: CommentPreviewSection | null = null;
  let preamble = '';
  let snapshot = '';
  let inSnapshot = false;
  // 失败/取消的复制内容在结论后自带“部分审查记录”说明；它不是章节，
  // 有前言时按原顺序保留在前言中，否则并入首个章节正文。
  let pendingNote = '';
  const start = tokens.indexOf(verdict!) + 1;
  for (let index = start; index < tokens.length; index += 1) {
    const token = tokens[index]!;
    if (!section && token.type === 'blockquote' && /部分审查记录/.test(token.text)) {
      pendingNote += token.raw;
      continue;
    }
    if (token.type === 'hr' && /^\s*本(?:结论|记录)基于/.test(tokens.slice(index + 1).map(item => item.raw).join(''))) {
      inSnapshot = true;
      continue;
    }
    if (inSnapshot) { snapshot += token.raw; continue; }
    if (token.type === 'heading' && token.depth === 2) {
      // 历史评论使用“阻断问题/建议项”等标题；统一结构后按“需要处理的问题/其他建议”等章节识别。
      const label = ({ '发现的问题': '已确认问题', '其他审查结果': '审查局限',
        '阻断问题': '需要处理的问题', '高优先级问题': '需要处理的问题', '代码证据已确认的问题': '已确认问题',
        '建议项': '其他建议', '建议': '其他建议', '变更概述': '变更理解',
        '其他局限': '仍需确认的事项', '未检查/未核实事项': '仍需确认的事项', '合并前提醒': '仍需确认的事项',
        '已保留记录': '变更理解', '未覆盖范围': '仍需确认的事项', '重新审查': '下一步',
      } as Record<string, string>)[token.text] || token.text;
      section = { title: label, markdown: pendingNote, findings: label === '已确认问题' };
      pendingNote = '';
      sections.push(section);
    } else if (section) {
      // Older comments used this label for the same source-bound observation.
      const raw = section.title === '变更理解' && token.type === 'paragraph'
        ? token.raw.replace(/^\*\*实际变更[：:]\*\*/, '**代码观察：**') : token.raw;
      section.markdown += raw;
    } else if (token.type !== 'space') {
      preamble += pendingNote + token.raw;
      pendingNote = '';
    } else if (!section) {
      if (pendingNote) pendingNote += token.raw;
      else preamble += token.raw;
    }
  }
  if (!sections.length) return null;
  for (const section of sections) {
    if (!["需要处理的问题", "其他建议"].includes(section.title)) continue;
    const count = marked.lexer(section.markdown).filter(token => token.type === "paragraph"
      && /^\*\*\[(Critical|High|Medium|Low)(?:\s|·|\])/.test(token.text)).length;
    // 历史“建议”章节可能只有行动文案；没有条目时不显示计数徽标。
    if (count || section.title !== "其他建议") {
      section.findings = true;
      section.findingCount = count;
    }
  }
  return { title, conclusion: match[2]!, positive: match[1] === '✅', preamble: preamble.trim(), sections, snapshot: snapshot.trim() };
}
