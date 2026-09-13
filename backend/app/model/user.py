from .base_model import Base
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String

class User(Base):
    """用户角色表"""
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True, comment="用户ID")
    name: Mapped[str] = mapped_column("name", __type_pos=String(50), comment="用户名")
    role: Mapped[str] = mapped_column("role", __type_pos=String(50), comment="用户角色")