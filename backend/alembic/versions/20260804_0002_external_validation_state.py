"""持久化 Project CI 与 User Runner 外部验证协调状态。"""

from alembic import op

from app.models.orm import (
    ProjectCIRequestOrm,
    ProjectCIWebhookDeliveryOrm,
    RunnerRegistrationOrm,
    RunnerResultIdempotencyOrm,
    UserValidationRequestOrm,
)

revision = "20260804_0002"
down_revision = "20260801_0001"
branch_labels = None
depends_on = None

_TABLES = (
    RunnerRegistrationOrm.__table__,
    UserValidationRequestOrm.__table__,
    RunnerResultIdempotencyOrm.__table__,
    ProjectCIRequestOrm.__table__,
    ProjectCIWebhookDeliveryOrm.__table__,
)


def upgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        table.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(_TABLES):
        table.drop(bind=bind, checkfirst=True)
