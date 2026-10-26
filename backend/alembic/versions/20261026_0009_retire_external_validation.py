"""Retire external validation, candidate patches and their coordination tables."""
from alembic import op
import sqlalchemy as sa

revision = "20261026_0009"
down_revision = "20261024_0008"
branch_labels = None
depends_on = None

_TABLES = (
    "project_ci_webhook_deliveries",
    "project_ci_requests",
    "runner_result_idempotency",
    "user_validation_requests",
    "runner_registrations",
    "validations",
    "patch_issue_links",
    "patches",
)


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in _TABLES:
        if inspector.has_table(table):
            op.drop_table(table)


def downgrade():
    # 已退役能力不重建表结构，历史验证数据不再恢复。
    pass
