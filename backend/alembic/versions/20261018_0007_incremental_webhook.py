"""增量审查 webhook 投递幂等记录。"""
from alembic import op
import sqlalchemy as sa

revision = "20261018_0007"
down_revision = "20261017_0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("incremental_webhook_deliveries",
        sa.Column("delivery_id", sa.String(128), primary_key=True),
        sa.Column("repository", sa.String(512), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("task_id", sa.String(32)),
        sa.Column("reason", sa.String(120)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_incremental_webhook_status", "incremental_webhook_deliveries",
                    ["status"])


def downgrade():
    op.drop_index("ix_incremental_webhook_status", table_name="incremental_webhook_deliveries")
    op.drop_table("incremental_webhook_deliveries")
