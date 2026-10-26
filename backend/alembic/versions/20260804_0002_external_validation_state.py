"""历史占位：外部验证协调表已退役，不再创建。

保留修订号以维持既有数据库的迁移链；新库不会创建这些表。
"""

revision = "20260804_0002"
down_revision = "20260801_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
