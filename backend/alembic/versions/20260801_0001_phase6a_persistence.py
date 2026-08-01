"""阶段 6A：任务、Unit、产物、人工请求、队列和幂等副作用持久化。"""

from alembic import op

from app.core.database import Base
from app.models import orm  # noqa: F401

revision = "20260801_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=True)
