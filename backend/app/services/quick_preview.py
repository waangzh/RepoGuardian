"""仅远端读取的快速概览，绕过克隆锁、完整作业队列和模型。"""
import asyncio

from app.models.quick_preview import QuickPreviewFile, QuickPreviewResponse
from app.tools.github_tool import parse_pr_url


class QuickPreviewStale(ValueError):
    pass


async def quick_preview(github, pr_url):
    parse_pr_url(pr_url)
    async with asyncio.timeout(45):
        before = await github.fetch_pr(pr_url)
        files, complete = await github.fetch_file_overview(pr_url)
        after = await github.fetch_pr(pr_url)
    if (before.base.sha, before.head.sha) != (after.base.sha, after.head.sha):
        raise QuickPreviewStale("PR快照已变化，请重新获取概览。")
    return QuickPreviewResponse(pr_url=before.html_url, repository=f"{before.owner}/{before.repo}",
        number=before.number, title=before.title, head_sha=before.head.sha, declared_base_sha=before.base.sha,
        files=[QuickPreviewFile(path=item["filename"], previous_path=item.get("previous_filename"),
            status=item["status"], additions=item["additions"], deletions=item["deletions"]) for item in files],
        files_complete=complete)
