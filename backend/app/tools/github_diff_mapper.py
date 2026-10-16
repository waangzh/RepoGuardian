"""只映射当前 GitHub 完整 patch 中已验证的真实行号，不使用 position。"""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class MappingFailure:
    reason: str


class GitHubDiffMapper:
    def _lines(self, file):
        patch = file.get("patch")
        if not isinstance(patch, str) or len(patch) > 1_000_000:
            return None
        lines, left, right, remaining_left, remaining_right, added, deleted = set(), 0, 0, 0, 0, 0, 0
        started = False
        for line in patch.splitlines():
            match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)$", line)
            if match:
                if remaining_left or remaining_right:
                    return None
                left, right = int(match[1]), int(match[3])
                remaining_left, remaining_right = int(match[2] or 1), int(match[4] or 1)
                started = True
                continue
            if line == "\\ No newline at end of file":
                continue
            if not started or not line or line[0] not in " +-":
                return None
            if line[0] in " -":
                remaining_left -= 1
                if line[0] == "-":
                    lines.add(("LEFT", left))
                    deleted += 1
                left += 1
            if line[0] in " +":
                remaining_right -= 1
                lines.add(("RIGHT", right))
                added += line[0] == "+"
                right += 1
            if remaining_left < 0 or remaining_right < 0:
                return None
        if not started or remaining_left or remaining_right or added != file.get("additions") or deleted != file.get("deletions"):
            return None
        return lines

    def map_finding(self, finding, files):
        anchor = finding.anchor
        candidates = [file for file in files if file.get("filename") == anchor.file_path or
                      (anchor.side == "base" and file.get("status") == "renamed" and file.get("previous_filename") == anchor.file_path)]
        if len(candidates) != 1 or anchor.side not in {"head", "base"} or not anchor.start_line:
            return MappingFailure("diff_mapping_unavailable")
        file = candidates[0]
        lines = self._lines(file)
        if lines is None:
            return MappingFailure("diff_mapping_unavailable")
        side = "RIGHT" if anchor.side == "head" else "LEFT"
        if (side, anchor.start_line) not in lines:
            return MappingFailure("diff_line_not_commentable")
        if anchor.end_line and anchor.end_line < anchor.start_line:
            return MappingFailure("diff_range_invalid")
        return {"path": file["filename"], "line": anchor.start_line, "side": side}
