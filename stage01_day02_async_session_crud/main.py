"""Stage 1 Day 2：补全 TODO，完成一次异步 CRUD。"""

import asyncio
import os
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.model.user import User


DATABASE_URL = "mysql+aiomysql://root:123456@192.168.0.229:3306/exc?charset=utf8mb4",


async def main() -> None:
    engine = create_async_engine(DATABASE_URL, echo=True)

    # TODO 1：使用 async_sessionmaker 创建会话工厂 SessionLocal。
    SessionLocal = None

    test_name = f"practice_{uuid.uuid4().hex[:8]}"

    try:
        # TODO 2：用 async with SessionLocal() as session 创建会话。
        # 以下全部操作都放在同一个会话代码块中。

        # CREATE：创建 User(name=test_name, role="viewer")，add、commit、refresh。
        # 保存生成的 user.id，并打印新增结果。

        # READ：执行 select(User).where(User.id == user_id)。
        # 使用 scalar_one_or_none() 取得对象并打印。

        # UPDATE：把查询到的用户 role 改为 "admin"，然后 commit、refresh 并打印。

        # DELETE：调用 await session.delete(user)，然后 commit。

        # VERIFY：再次按 ID 查询并断言结果是 None。
        # print("删除后查询结果:", deleted_user)
        # assert deleted_user is None
        # print("CRUD 练习通过")
        raise NotImplementedError("请按任务说明补全 CRUD TODO")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
