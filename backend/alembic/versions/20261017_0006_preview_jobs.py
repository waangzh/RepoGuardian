"""可恢复的预览作业与固定快照输入缓存。"""
from alembic import op
import sqlalchemy as sa

revision = "20261017_0006"
down_revision = "20261016_0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("preview_jobs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("stages", sa.JSON(), nullable=False),
        sa.Column("preview_id", sa.String(32)), sa.Column("error", sa.String(512)),
        sa.Column("cache_hit", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("lease_owner", sa.String(32)), sa.Column("lease_until", sa.DateTime(timezone=True)))
    op.create_index("ix_preview_jobs_status", "preview_jobs", ["status"])
    op.create_table("preview_input_cache",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table("preview_input_cache")
    op.drop_index("ix_preview_jobs_status", table_name="preview_jobs")
    op.drop_table("preview_jobs")
