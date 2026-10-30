"""Fresh vNext database. Existing databases must be archived, never upgraded in place."""

from alembic import op
import sqlalchemy as sa

from app.core.database import Base
from app.models import orm  # noqa: F401

revision = "20261031_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    if set(sa.inspect(op.get_bind()).get_table_names()) - {"alembic_version"}:
        raise RuntimeError("Initialize vNext in a fresh database; existing data was not modified.")
    Base.metadata.create_all(op.get_bind(), checkfirst=False)


def downgrade():
    raise RuntimeError("Archive this database and select the previous database to roll back.")
