"""Calibrate input reservations from usage and hold each branch's final submission."""

from alembic import op
import sqlalchemy as sa

revision = "20261028_0004"
down_revision = "20261027_0003"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("review_model_calls") as batch:
        batch.alter_column("reserved_input_tokens", new_column_name="reserved_input_bytes")
        batch.add_column(sa.Column("held_input_tokens", sa.Integer(), nullable=False,
                                   server_default="0"))


def downgrade():
    with op.batch_alter_table("review_model_calls") as batch:
        batch.drop_column("held_input_tokens")
        batch.alter_column("reserved_input_bytes", new_column_name="reserved_input_tokens")
