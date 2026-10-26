"""Current native graph business schema. Historical runtime data is intentionally unsupported."""
from alembic import op

from app.core.database import Base
from app.models import orm  # noqa: F401

revision = "20261027_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=False)


def downgrade():
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=True)
