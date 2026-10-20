"""Bounded read-only collection; partial sources never become empty successes."""

import asyncio
from datetime import datetime, timezone

import httpx

from app.models.pr_discussion import (
    DiscussionComment, DiscussionPolicy, DiscussionSnapshot, DiscussionThread,
)


THREAD_QUERY = """
query($owner:String!,$repo:String!,$number:Int!,$cursor:String) {
  repository(owner:$owner,name:$repo) {
    pullRequest(number:$number) {
      reviewThreads(first:50,after:$cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { id path isResolved isOutdated
          comments(first:50) {
            pageInfo { hasNextPage endCursor }
            nodes { databaseId body url createdAt updatedAt author { login }
              commit { oid } originalCommit { oid } line originalLine diffHunk }
          }
        }
      }
    }
  }
}
"""
REPLIES_QUERY = """
query($id:ID!,$cursor:String) {
  node(id:$id) { ... on PullRequestReviewThread {
    comments(first:50,after:$cursor) {
      pageInfo { hasNextPage endCursor }
      nodes { databaseId body url createdAt updatedAt author { login }
        commit { oid } originalCommit { oid } line originalLine diffHunk }
    }
  } }
}
"""


class CollectionLimit(ValueError):
    pass


class GitHubDiscussionReader:
    def __init__(self, github, policy=None):
        self.github = github
        self.policy = policy or DiscussionPolicy()
        self.threads = {}
        self.count = 0
        self.bytes = 0
        self.sources = {key: "unavailable" for key in ("conversation", "review", "inline")}
        self.limitations = []

    def add(self, thread, comment):
        existing = self.threads.get(thread.id)
        if existing and any(item.id == comment.id for item in existing.comments):
            return
        size = len(comment.model_dump_json().encode("utf-8"))
        if ((existing is None and len(self.threads) >= self.policy.max_roots)
                or self.count >= self.policy.max_comments or self.bytes + size > self.policy.max_bytes):
            raise CollectionLimit("collection_limit")
        if existing is None:
            thread.comments = []
            self.threads[thread.id] = thread
            existing = thread
        existing.comments.append(comment)
        self.count += 1
        self.bytes += size

    async def graphql(self, query, variables):
        headers = {"Authorization": f"Bearer {getattr(self.github, "_token", None)}", "Accept": "application/vnd.github+json"}
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post("https://api.github.com/graphql",
                                         headers=headers, json={"query": query, "variables": variables})
        response.raise_for_status()
        if len(response.content) > self.policy.max_bytes:
            raise CollectionLimit("response_limit")
        value = response.json()
        if not isinstance(value, dict) or value.get("errors"):
            raise ValueError("graphql_unavailable")
        return value["data"]

    async def rest(self, owner, repo, number, suffix):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if getattr(self.github, "_token", None):
            headers["Authorization"] = f"Bearer {getattr(self.github, "_token", None)}"
        page = 1
        while True:
            response = await self.github._get(
                f"https://api.github.com/repos/{owner}/{repo}/{suffix.format(number=number)}",
                headers, params={"per_page": self.policy.page_size, "page": page})
            if len(response.content) > self.policy.max_bytes:
                raise CollectionLimit("response_limit")
            values = response.json()
            if not isinstance(values, list):
                raise ValueError("invalid_comments")
            for item in values:
                if not isinstance(item, dict) or not isinstance(item.get("body"), str):
                    raise ValueError("invalid_comment")
                yield item
            if len(values) < self.policy.page_size and "next" not in response.links:
                break
            page += 1

    @staticmethod
    def comment(raw, *, graphql=False):
        if graphql:
            return DiscussionComment(id=f"comment:{raw['databaseId']}", body=raw["body"],
                author=(raw.get("author") or {}).get("login", ""), url=raw.get("url"),
                created_at=raw.get("createdAt"), updated_at=raw.get("updatedAt"),
                commit_id=(raw.get("commit") or {}).get("oid"),
                original_commit_id=(raw.get("originalCommit") or {}).get("oid"),
                line=raw.get("line"), original_line=raw.get("originalLine"), diff_hunk=raw.get("diffHunk"))
        return DiscussionComment(id=f"comment:{raw['id']}", body=raw["body"],
            author=(raw.get("user") or {}).get("login", ""), url=raw.get("html_url"),
            created_at=raw.get("created_at") or raw.get("submitted_at"), updated_at=raw.get("updated_at"),
            commit_id=raw.get("commit_id"), original_commit_id=raw.get("original_commit_id"),
            line=raw.get("line"), original_line=raw.get("original_line"), side=raw.get("side"),
            diff_hunk=raw.get("diff_hunk"))

    async def simple(self, owner, repo, number, source, suffix):
        async for raw in self.rest(owner, repo, number, suffix):
            if not raw["body"].strip():
                continue
            self.add(DiscussionThread(id=f"{source}:{raw['id']}", source=source, comments=[]), self.comment(raw))
        self.sources[source] = "complete"

    async def inline(self, owner, repo, number):
        try:
            if not getattr(self.github, "_token", None):
                raise ValueError("graphql_auth_unavailable")
            cursor = None
            seen = set()
            while True:
                data = await self.graphql(THREAD_QUERY, dict(owner=owner, repo=repo, number=number, cursor=cursor))
                connection = data["repository"]["pullRequest"]["reviewThreads"]
                for node in connection["nodes"]:
                    comments = node["comments"]
                    if not comments["nodes"]:
                        continue
                    thread = DiscussionThread(id=f"inline:{comments['nodes'][0]['databaseId']}",
                        source="inline", comments=[], path=node.get("path"),
                        resolved=node.get("isResolved"), outdated=node.get("isOutdated"), complete=False)
                    reply_seen = set()
                    while True:
                        for raw in comments["nodes"]:
                            self.add(thread, self.comment(raw, graphql=True))
                        if not comments["pageInfo"]["hasNextPage"]:
                            self.threads[thread.id].complete = True
                            break
                        reply_cursor = comments["pageInfo"]["endCursor"]
                        if not reply_cursor or reply_cursor in reply_seen:
                            raise ValueError("repeated_reply_cursor")
                        reply_seen.add(reply_cursor)
                        data = await self.graphql(REPLIES_QUERY, dict(id=node["id"], cursor=reply_cursor))
                        comments = data["node"]["comments"]
                if not connection["pageInfo"]["hasNextPage"]:
                    self.sources["inline"] = "complete"
                    return
                cursor = connection["pageInfo"]["endCursor"]
                if not cursor or cursor in seen:
                    raise ValueError("repeated_thread_cursor")
                seen.add(cursor)
        except CollectionLimit:
            raise
        except (ValueError, KeyError, TypeError, httpx.HTTPError):
            self.limitations.append("inline_thread_state_unavailable")
        parents = {}
        async for raw in self.rest(owner, repo, number, "pulls/{number}/comments"):
            identity = raw["id"]
            parents[identity] = raw.get("in_reply_to_id", identity)
            root, visited = identity, set()
            while root in parents and parents[root] != root:
                if root in visited:
                    raise ValueError("cyclic_comment_reply")
                visited.add(root)
                root = parents[root]
            self.add(DiscussionThread(id=f"inline:{root}", source="inline", comments=[],
                                     path=raw.get("path")), self.comment(raw))
        # REST has no authoritative resolved/outdated thread state.
        self.sources["inline"] = "partial"

    async def collect(self, *, task_id, pr_url, head_sha, base_sha, owner, repo, number):
        started = datetime.now(timezone.utc).isoformat()

        async def source(name, operation):
            try:
                await operation
            except (ValueError, KeyError, TypeError, httpx.HTTPError, RuntimeError, AttributeError) as exc:
                self.sources[name] = "partial" if any(t.source == name for t in self.threads.values()) else "unavailable"
                self.limitations.append(f"{name}:" + ("collection_limit" if isinstance(exc, CollectionLimit)
                                                     else "collection_failed"))
        try:
            async with asyncio.timeout(self.policy.timeout_seconds):
                await asyncio.gather(
                    source("conversation", self.simple(owner, repo, number, "conversation", "issues/{number}/comments")),
                    source("review", self.simple(owner, repo, number, "review", "pulls/{number}/reviews")),
                    source("inline", self.inline(owner, repo, number)))
        except TimeoutError:
            self.limitations.append("collection_timeout")
        for key in self.sources:
            if self.sources[key] == "unavailable" and any(t.source == key for t in self.threads.values()):
                self.sources[key] = "partial"
        return DiscussionSnapshot(task_id=task_id, pr_url=pr_url, head_sha=head_sha, base_sha=base_sha,
            started_at=started, finished_at=datetime.now(timezone.utc).isoformat(), sources=self.sources,
            limitations=self.limitations, threads=list(self.threads.values()))
