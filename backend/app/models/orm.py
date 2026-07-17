"""SQLAlchemy ORM 模型 —— 数据库表映射。

当前版本未在审查管道中使用，保留供将来多用户持久化场景。
表关系：ReviewTaskOrm 1:1 RepoSnapshotOrm, 1:N ChangedFileOrm / CodeSymbolOrm
        / ReviewIssueOrm / PatchOrm / TestRunOrm
"""

import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class ReviewTaskOrm(Base):
    """审查任务表。"""
    __tablename__ = "review_tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    repo_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    mode: Mapped[str] = mapped_column(String(32), default="pr_review")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    checkpoint_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
        onupdate=lambda: datetime.datetime.now(datetime.timezone.utc),
    )

    snapshot: Mapped["RepoSnapshotOrm | None"] = relationship(
        back_populates="task", uselist=False, cascade="all, delete-orphan"
    )
    changed_files: Mapped[list["ChangedFileOrm"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    symbols: Mapped[list["CodeSymbolOrm"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    issues: Mapped[list["ReviewIssueOrm"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    patches: Mapped[list["PatchOrm"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    test_runs: Mapped[list["TestRunOrm"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class RepoSnapshotOrm(Base):
    """仓库快照表（1:1 关联 ReviewTaskOrm）。"""
    __tablename__ = "repo_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(32), ForeignKey("review_tasks.id"), unique=True)
    local_path: Mapped[str] = mapped_column(String(1024))
    base_commit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    head_commit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    framework: Mapped[str | None] = mapped_column(String(64), nullable=True)
    test_framework: Mapped[str | None] = mapped_column(String(64), nullable=True)

    task: Mapped[ReviewTaskOrm] = relationship(back_populates="snapshot")


class ChangedFileOrm(Base):
    """变更文件表（N:1 关联 ReviewTaskOrm）。"""
    __tablename__ = "changed_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(32), ForeignKey("review_tasks.id"), index=True)
    file_path: Mapped[str] = mapped_column(String(512))
    change_type: Mapped[str] = mapped_column(String(16))
    additions: Mapped[int] = mapped_column(Integer, default=0)
    deletions: Mapped[int] = mapped_column(Integer, default=0)
    hunks_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # DiffHunk 序列化 JSON

    task: Mapped[ReviewTaskOrm] = relationship(back_populates="changed_files")


class CodeSymbolOrm(Base):
    """代码符号表（函数/类/方法）。"""
    __tablename__ = "code_symbols"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(32), ForeignKey("review_tasks.id"), index=True)
    file_path: Mapped[str] = mapped_column(String(512))
    symbol_name: Mapped[str] = mapped_column(String(256))
    symbol_type: Mapped[str] = mapped_column(String(32))  # function / class / method
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    signature: Mapped[str | None] = mapped_column(String(512), nullable=True)
    docstring: Mapped[str | None] = mapped_column(Text, nullable=True)

    task: Mapped[ReviewTaskOrm] = relationship(back_populates="symbols")


class ReviewIssueOrm(Base):
    """审查问题表。"""
    __tablename__ = "review_issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(32), ForeignKey("review_tasks.id"), index=True)
    file_path: Mapped[str] = mapped_column(String(512))
    line_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    severity: Mapped[str] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(Text)
    suggestion: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    auto_fixable: Mapped[bool] = mapped_column(default=False)

    task: Mapped[ReviewTaskOrm] = relationship(back_populates="issues")
    patches: Mapped[list["PatchOrm"]] = relationship(back_populates="issue")


class PatchOrm(Base):
    """自动修复 patch 表。"""
    __tablename__ = "patches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(32), ForeignKey("review_tasks.id"), index=True)
    issue_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("review_issues.id"), nullable=True)
    diff_content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="unverified")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )

    task: Mapped[ReviewTaskOrm] = relationship(back_populates="patches")
    issue: Mapped["ReviewIssueOrm | None"] = relationship(back_populates="patches")
    test_runs: Mapped[list["TestRunOrm"]] = relationship(back_populates="patch")


class TestRunOrm(Base):
    """测试/静态分析运行记录表。"""
    __tablename__ = "test_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(32), ForeignKey("review_tasks.id"), index=True)
    patch_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("patches.id"), nullable=True)
    command: Mapped[str] = mapped_column(String(512))
    exit_code: Mapped[int] = mapped_column(Integer)
    stdout: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    passed: Mapped[bool] = mapped_column(default=False)

    task: Mapped[ReviewTaskOrm] = relationship(back_populates="test_runs")
    patch: Mapped["PatchOrm | None"] = relationship(back_populates="test_runs")
