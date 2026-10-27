"""Remove the unused prepared-input payload cache."""

from alembic import op
import sqlalchemy as sa

revision = "20261027_0002"
down_revision = "20261027_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table("preview_input_cache", if_exists=True)


def downgrade():
    op.create_table(
        "preview_input_cache",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
