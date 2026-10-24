import hashlib


def evidence_catalog(*identities):
    content = "return value"
    return {identity: {"file_path": "src/a.py", "start_line": 1, "end_line": 1,
                      "content": content, "content_hash": hashlib.sha256(content.encode()).hexdigest(),
                      "head_sha": "a" * 40, "base_sha": "b" * 40}
            for identity in identities}
