"""Stage 2 并发实验用的表。

刻意使用独立的 ``LabBase``，**不复用** ``backend/app/model/base_model.py``：
实验表不应该混进项目 schema，免得后面做 Alembic 迁移时多出一张莫名其妙的表。

Day 2～Day 5 的锁实验都会继续用这张 ``lock_lab_counter``。
"""

from sqlalchemy import Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class LabBase(DeclarativeBase):
    pass


class LockLabCounter(LabBase):
    """一个极简的计数行，用来放大并发读-改-写的竞态。"""

    __tablename__ = "lock_lab_counter"

    id: Mapped[int] = mapped_column(primary_key=True, comment="主键")
    name: Mapped[str] = mapped_column(String(64), unique=True, comment="实验名，用于隔离不同天的数据")
    value: Mapped[int] = mapped_column(Integer, default=0, comment="计数值")
