from ..model.user import User
from sqlalchemy_crud_plus import CRUDPlus, JoinConfig
from sqlalchemy.ext.asyncio import AsyncSession

class CRUDUser(CRUDPlus[User]):
    """用户操作库的操作类"""

