"""Register disposable prepared Git workspaces."""
from alembic import op
import sqlalchemy as sa

revision = "20261024_0008"
down_revision = "20261018_0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("prepared_workspaces",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("input_key", sa.String(64), nullable=False),
        sa.Column("directory", sa.String(64), nullable=False, unique=True),
        sa.Column("base_sha", sa.String(64), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("effective_base_sha", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_prepared_workspaces_input_key", "prepared_workspaces", ["input_key"])


def downgrade():
    op.drop_index("ix_prepared_workspaces_input_key", table_name="prepared_workspaces")
    op.drop_table("prepared_workspaces")
