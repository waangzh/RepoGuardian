"""独立的发布任务、请求幂等身份、投递对象、批次与 PR Summary Slot。"""
from alembic import op
from app.models.orm import (PublicationAttemptOrm, PublicationRequestOrm, PublicationItemOrm,
                            PublicationBatchOrm, GitHubSummarySlotOrm)

revision = "20261016_0004"
down_revision = "20261016_0003"
branch_labels = None
depends_on = None
_TABLES = (PublicationAttemptOrm.__table__, PublicationRequestOrm.__table__, PublicationItemOrm.__table__,
           PublicationBatchOrm.__table__, GitHubSummarySlotOrm.__table__)


def upgrade():
    for table in _TABLES:
        table.create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    for table in reversed(_TABLES):
        table.drop(bind=op.get_bind(), checkfirst=True)
