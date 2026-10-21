"""Conservative publication defaults; related discussions never remove a risk."""

from urllib.parse import urlsplit


def valid_matches(matches, discussions, path):
    output = []
    for match in matches:
        thread = next((d for d in discussions if d["id"] == match.discussion_id), None)
        if not thread or thread.get("path") != path:
            continue
        comment = next((c for c in thread["comments"] if c["id"] == match.comment_id), None)
        quote = (thread.get("summary") or {}).get("quotes", {}).get(match.comment_id, "")
        if not comment or (match.quote not in comment["body"] and match.quote not in quote):
            continue
        if not match.quote.strip():
            continue
        output.append(match)
    return output


def exact_duplicate_matches(matches):
    """只有主体、触发条件、影响都相同且没有新信息时，才视为同一缺陷的重复记录。

    文件路径相同或标题相似都不足以去重；这里只读取执行器已保存的结构化匹配元数据。
    """
    return [match for match in matches or []
            if isinstance(match, dict) and match.get("same_subject") is True
            and match.get("same_trigger") is True and match.get("same_impact") is True
            and match.get("new_information") is False]


def finding_discussions(record):
    matches = record.get("verification", {}).get("discussion_matches") or []
    duplicate = bool(exact_duplicate_matches(matches))
    return matches, duplicate


def safe_discussion_url(value, pr_url):
    try:
        parsed = urlsplit(value or "")
        expected = urlsplit(pr_url or "")
        if (parsed.scheme == "https" and parsed.netloc == "github.com" and not parsed.query
                and parsed.path == expected.path and parsed.fragment
                and parsed.fragment.startswith(("discussion_r", "issuecomment-", "pullrequestreview-"))):
            return value
    except ValueError:
        pass
    return None
