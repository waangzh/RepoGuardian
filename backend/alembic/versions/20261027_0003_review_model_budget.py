"""Durable whole-review model request reservations."""

from alembic import op
import sqlalchemy as sa

revision = "20261027_0003"
down_revision = "20261027_0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("review_model_calls",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("task_id", sa.String(32), sa.ForeignKey("review_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_id", sa.String(160), nullable=False),
        sa.Column("reserved_input_tokens", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer()), sa.Column("output_tokens", sa.Integer()),
        sa.Column("cached_input_tokens", sa.Integer()),
        sa.Column("settled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_review_model_calls_task_id", "review_model_calls", ["task_id"])


def downgrade():
    op.drop_index("ix_review_model_calls_task_id", table_name="review_model_calls")
    op.drop_table("review_model_calls")
