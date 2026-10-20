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


def finding_discussions(record):
    matches = record.get("verification", {}).get("discussion_matches") or []
    duplicate = any(m.get("same_subject") is True and m.get("same_trigger") is True
                    and m.get("same_impact") is True and m.get("new_information") is False for m in matches)
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
