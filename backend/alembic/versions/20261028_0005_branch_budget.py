"""Keep final submission reservations and phase with their owning branch."""

from alembic import op
import sqlalchemy as sa

revision = "20261028_0005"
down_revision = "20261028_0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("review_branch_budgets",
        sa.Column("task_id", sa.String(32), sa.ForeignKey("review_tasks.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("agent_id", sa.String(160), primary_key=True),
        sa.Column("held_input_tokens", sa.Integer(), nullable=False),
        sa.Column("held_model_calls", sa.Integer(), nullable=False),
        sa.Column("finalizing", sa.Boolean(), nullable=False),
        sa.Column("final_requests", sa.Integer(), nullable=False))
    # Existing outstanding holds remain occupied; recover them conservatively in final-only mode.
    op.execute(sa.text("""
        INSERT INTO review_branch_budgets
            (task_id, agent_id, held_input_tokens, held_model_calls, finalizing, final_requests)
        SELECT task_id, agent_id, SUM(held_input_tokens), 2, 1, 0
        FROM review_model_calls WHERE settled = 0 AND held_input_tokens > 0
        GROUP BY task_id, agent_id
    """))
    with op.batch_alter_table("review_model_calls") as batch:
        batch.drop_column("held_input_tokens")


def downgrade():
    with op.batch_alter_table("review_model_calls") as batch:
        batch.add_column(sa.Column("held_input_tokens", sa.Integer(), nullable=False, server_default="0"))
    op.execute(sa.text("""
        UPDATE review_model_calls SET held_input_tokens = COALESCE((
            SELECT held_input_tokens FROM review_branch_budgets AS branch
            WHERE branch.task_id = review_model_calls.task_id
              AND branch.agent_id = review_model_calls.agent_id
        ), 0) WHERE id IN (
            SELECT (SELECT calls.id FROM review_model_calls AS calls
                    WHERE calls.task_id = branch.task_id AND calls.agent_id = branch.agent_id
                      AND calls.settled = 0 ORDER BY calls.created_at DESC, calls.id DESC LIMIT 1)
            FROM review_branch_budgets AS branch
        )
    """))
    op.drop_table("review_branch_budgets")
