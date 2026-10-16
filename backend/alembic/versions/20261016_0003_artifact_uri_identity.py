"""移除历史产物的全局内容唯一约束，保留任务目录中的 URI 身份。"""

from alembic import op
from sqlalchemy import inspect

revision = "20261016_0003"
down_revision = "20260804_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    constraints = inspect(op.get_bind()).get_unique_constraints("artifacts")
    legacy = next((item for item in constraints if item["name"] == "uq_artifacts_hash_kind"), None)
    if legacy is None:
        return
    if legacy["column_names"] != ["sha256", "kind"]:
        raise RuntimeError("unexpected uq_artifacts_hash_kind columns")
    with op.batch_alter_table("artifacts") as batch:
        batch.drop_constraint("uq_artifacts_hash_kind", type_="unique")


def downgrade() -> None:
    # 0002 的当前 ORM 同样只要求 URI 唯一；不能给已合法重复的内容强加旧约束。
    # 完整恢复历史环境应使用升级前备份，不删除或合并任务产物。
    pass
