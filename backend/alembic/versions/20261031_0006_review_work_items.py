"""Persist frozen work, candidates, create intents and request tickets."""

from alembic import op

revision = "20261031_0006"
down_revision = "20261028_0005"
branch_labels = None
depends_on = None


def upgrade():
    from app.models.orm import (
        ReviewCandidateOrm, ReviewCreateIntentOrm, ReviewRequestOrm, ReviewWorkItemOrm,
    )
    for model in (ReviewCreateIntentOrm, ReviewWorkItemOrm, ReviewCandidateOrm, ReviewRequestOrm):
        model.__table__.create(op.get_bind())


def downgrade():
    for table in ("review_requests", "review_candidates", "review_work_items", "review_create_intents"):
        op.drop_table(table)
