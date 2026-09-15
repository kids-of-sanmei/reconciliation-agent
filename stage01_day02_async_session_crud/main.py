"""Stage 1 Day 2：补全 TODO，完成一次异步 CRUD。"""

import asyncio
import os
import uuid
from typing import Sequence # 表示“按顺序排列的一组元素”

from Tools.scripts.summarize_stats import pre_succ_pairs_section
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession
from sqlalchemy.engine import Result, RowMapping
from backend.app.model.user import User


DATABASE_URL = "mysql+aiomysql://root:123456@192.168.0.229:3306/exc?charset=utf8mb4"


async def main() -> None:
    async_engine = create_async_engine(
        DATABASE_URL,
        echo=True,
        pool_size=10,
        max_overflow=20
    )

    # TODO 1：使用 async_sessionmaker 创建会话工厂 async_session_factory。
    async_session_factory = async_sessionmaker(
        bind = async_engine,
        class_=AsyncSession,
        expire_on_commit=False, # 事务提交后对象属性是否失效
    )

    unique_test_name = f"practice_{uuid.uuid4().hex[:8]}"

    try:
        # TODO 2：用 async with async_session_factory() as db_session 创建会话。
        """增加操作"""
        async with async_session_factory() as session:
            user_add = User(name="byj", role="admin")
            session.add(user_add)
            await session.commit()

        """查询操作"""
        target_user_id:int = 1
        target_user_name:str = "A"
        # TODO 2.1: 条件查询操作
        async with async_session_factory() as db_session:
            id_query_result: Result[tuple[User]] = await db_session.execute(
                select(User).where(User.id == target_user_id)
            )
            user_by_id: User | None = await db_session.get(User, target_user_id)
            """scalars()只取每一行的第一列"""
            queried_users: Sequence[User] = id_query_result.scalars().all()
        # 使用 scalar_one_or_none() 取得对象并打印。
            single_user: User | None = id_query_result.scalar_one_or_none()

        # TODO 2.2: 模糊条件查询
        async with async_session_factory() as like_session:
            like_select = select(User).where(User.name.like("%b%"))
            like_result = (await like_session.execute(like_select)).scalars().all()

        # TODO 2.3: 聚合查询
        async with async_session_factory() as func_session:
            """COUNT：统计总行数，scalar_one() 直接取单个标量值"""
            count_result = await func_session.execute(
                select(func.count()).select_from(User)
            )
            total_count: int = count_result.scalar_one()

            """分组聚合：按 role 分组统计各角色人数"""
            group_result = await func_session.execute(
                select(User.role, func.count(User.id).label("user_count"))
                .group_by(User.role)
            )
            """等价于
            SELECT user.role, count(user.id) AS user_count
            FROM user
            GROUP BY user.role
            """

            """rows() 返回 (role, user_count) 元组，适合多列聚合结果"""
            role_stats: Sequence[tuple[str, int]] = group_result.all()

            """MIN/MAX：取 id 的最小值和最大值"""
            min_max_result = await func_session.execute(
                select(func.min(User.id), func.max(User.id))
            )
            min_id, max_id = min_max_result.one()

        # TODO 2.4: 分页查询
        page: int = 1
        page_size: int = 2
        async with async_session_factory() as limit_session:
            """分页三要素：order_by 保证顺序稳定，limit 每页条数，offset 跳过前几页"""
            page_select = (
                select(User)
                .order_by(User.id)
                .limit(page_size)
                .offset((page - 1) * page_size)
            )
            page_users: Sequence[User] = (await limit_session.execute(page_select)).scalars().all()

            """配合总条数算出总页数，是分页接口的常见写法"""
            total_result = await limit_session.execute(
                select(func.count()).select_from(User)
            )
            total: int = total_result.scalar_one()
            total_pages: int = (total + page_size - 1) // page_size  # 向上取整

        """更新操作"""
        async with async_session_factory() as db_session:
            name_query_result: Result[tuple[User]] = await db_session.execute(
                select(User).where(User.name == target_user_name)
            )
            users_to_update = name_query_result.scalars().all()
            for user_to_update in users_to_update:
                user_to_update.name = target_user_name + "B"

            await db_session.commit()
            await db_session.refresh(user_to_update)

        """删除操作"""
        async with async_session_factory() as db_session:
            user_to_delete: User | None = await db_session.get(User, target_user_id)
            await db_session.delete(user_to_delete)
            await db_session.commit()

        """自由操作"""
        from sqlalchemy import text
        sql = text("""
            SELECT *
            FROM user
            WHERE user_name = :name
        """)
        async with async_session_factory() as text_session:
            text_res: Result = await text_session.execute(
                sql,
                {
                    "name": "byj"
                }
            )
            text_final_res: Sequence[RowMapping] = text_res.mappings().all()


        raise NotImplementedError("请按任务说明补全 CRUD TODO")
    finally:
        await async_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
