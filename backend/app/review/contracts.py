"""HTTP boundaries for the single review architecture."""

from pydantic import Field, HttpUrl

from app.review.domain import Model


class ReviewRequest(Model):
    pr_url: HttpUrl
    depth: str = "standard"
    paths: list[str] | None = None
    focus: list[str] = Field(default_factory=lambda: [
        "correctness", "security", "performance", "compatibility"])
    limits: dict[str, int] = Field(default_factory=dict, max_length=10)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    include_pr_discussions: bool | None = None


class FindingDisposition(Model):
    status: str = Field(pattern=r"^(open|resolved|dismissed)$")
    note: str | None = Field(default=None, max_length=2000)
