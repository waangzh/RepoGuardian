"""阶段 6A：任务、Unit、产物、人工请求、队列和幂等副作用持久化。"""

from alembic import op

from app.core.database import Base
from app.models import orm  # noqa: F401

revision = "20260801_0001"
down_revision = None
branch_labels = None
depends_on = None

_ORIGINAL_TABLE_NAMES = (
    "review_tasks",
    "review_units",
    "review_issues",
    "patches",
    "patch_issue_links",
    "validations",
    "human_requests",
    "worker_jobs",
    "side_effects",
    "artifacts",
)


def _original_tables():
    return [Base.metadata.tables[name] for name in _ORIGINAL_TABLE_NAMES]


def upgrade() -> None:
    Base.metadata.create_all(
        bind=op.get_bind(), tables=_original_tables(), checkfirst=False
    )


def downgrade() -> None:
    Base.metadata.drop_all(
        bind=op.get_bind(), tables=_original_tables(), checkfirst=True
    )
