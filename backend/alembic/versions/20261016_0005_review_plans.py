"""持久化预览凭据与独立人工处理状态。"""
from alembic import op
import sqlalchemy as sa

revision = "20261016_0005"
down_revision = "20261016_0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("review_previews", sa.Column("id", sa.String(32), primary_key=True),
                    sa.Column("payload", sa.JSON(), nullable=False),
                    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("consumed_task_id", sa.String(32)))
    op.create_index("ix_review_previews_expires_at", "review_previews", ["expires_at"])
    op.create_table("finding_dispositions",
        sa.Column("task_id", sa.String(32), sa.ForeignKey("review_tasks.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("finding_id", sa.String(160), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table("finding_dispositions")
    op.drop_index("ix_review_previews_expires_at", table_name="review_previews")
    op.drop_table("review_previews")
