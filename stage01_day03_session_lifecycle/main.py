"""Stage 1 Day 3：观察 Session 生命周期 —— add / flush / commit / refresh / rollback。

用法：先读 daily_agent_tasks.md，对每个实验写下你的预测，再补全 TODO 运行验证。
预期结果写在 daily_agent_tasks.md 里，先别看，先猜。
"""

import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.model.user import User


DATABASE_URL = "mysql+aiomysql://root:123456@192.168.0.229:3306/exc?charset=utf8mb4"


def show(label: str, value: object) -> None:
    print(f"  {label}: {value!r}")


async def main() -> None:
    async_engine = create_async_engine(
        DATABASE_URL,
        echo=False,  # 今天先关掉 SQL 日志，专心看结果；最后一轮再打开对比
        pool_size=5,
        max_overflow=10,
    )
    async_session_factory = async_sessionmaker(
        bind=async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    # 本次实验的所有数据都用同一个名字，方便最后一次性清理
    test_name = f"lifecycle_{uuid.uuid4().hex[:8]}"
    committed_id: int | None = None  # 实验 3 产出的 id，供实验 4 使用

    try:
        # ==================== 实验 1：add() 只入队 ====================
        print("\n=== 实验 1：add() 之后、flush() 之前 ===")
        async with async_session_factory() as session:
            user = User(name=test_name, role="admin")

            # TODO 1: 把 user 加入 session（此时不应该有任何 SQL 发往数据库）

            show("user.id", user.id)
            show("user 在 session.new 里", user in session.new)

        # ==================== 实验 2：flush() 发送 SQL 但不提交 ====================
        print("\n=== 实验 2：flush() 之后、commit() 之前 ===")
        async with async_session_factory() as session:
            user = User(name=test_name, role="admin")
            session.add(user)

            # TODO 2: flush —— 把挂起的 INSERT 发出去，让数据库分配主键，但事务不结束

            flushed_id = user.id
            show("flush 后 user.id", flushed_id)

            # 另开一个全新会话（全新事务）去查同一条记录
            async with async_session_factory() as other_session:
                # TODO 3: 在 other_session 中查询 User.id == flushed_id 的记录，
                #         把结果赋给 other_user
                other_user = None

                show("另一个会话能看到这条数据吗", other_user is not None)

            # TODO 4: 回滚，丢弃实验数据（提示：这是 async 方法）

        # ==================== 实验 3：commit() 是可见性分界线 ====================
        print("\n=== 实验 3：commit() 之后 ===")
        async with async_session_factory() as session:
            user = User(name=test_name, role="admin")
            session.add(user)
            await session.flush()
            committed_id = user.id

            # TODO 5: 提交

            async with async_session_factory() as other_session:
                # TODO 6: 在 other_session 中按 committed_id 查询，赋给 other_user
                other_user = None

                show("另一个会话能看到这条数据吗", other_user is not None)
                show("另一个会话读到的 role", getattr(other_user, "role", None))

        # ==================== 实验 4：refresh() 会先 autoflush ====================
        # 4a 和 4b 都故意只改内存、不提交，退出 session 时自动回滚，不会污染数据
        print("\n=== 实验 4a：直接 refresh ===")
        async with async_session_factory() as session:
            # TODO 7: 用 session.get(User, committed_id) 把实验 3 那行取回来
            user = None

            user.role = "in_memory_only"
            show("只改内存后 role", user.role)

            # TODO 8: 调用 refresh，把 user 重新从数据库读一遍
            show("refresh 后 role", user.role)

        print("\n=== 实验 4b：加上 no_autoflush 再 refresh ===")
        async with async_session_factory() as session:
            user = await session.get(User, committed_id)
            user.role = "in_memory_only"
            show("只改内存后 role", user.role)

            # TODO 9: 用 with session.no_autoflush: 包住 refresh，再观察结果

            show("refresh 后 role", user.role)

        # ==================== 实验 5：rollback() 的边界 ====================
        print("\n=== 实验 5a：rollback 撤销未提交的 INSERT ===")
        async with async_session_factory() as session:
            user = User(name=test_name, role="admin")
            session.add(user)
            await session.flush()
            rolled_back_id = user.id
            show("flush 后 user.id", rolled_back_id)

            # TODO 10: 回滚

            show("rollback 后 user.id", user.id)

        async with async_session_factory() as session:
            # TODO 11: 确认数据库里已经查不到 rolled_back_id 这一行
            found = None

            show("数据库里还能查到吗", found is not None)

        print("\n=== 实验 5b（选做）：commit 之后再 rollback ===")
        async with async_session_factory() as session:
            user = User(name=test_name, role="admin")
            session.add(user)
            await session.commit()
            show("commit 后 user.id", user.id)

            # TODO 12: 在 commit 之后再调用一次 rollback，然后想想它能否撤销已提交的数据

        async with async_session_factory() as session:
            # TODO 13: 确认这行数据是否还在
            still_there = None

            show("commit 后再 rollback，数据还在吗", still_there is not None)

        # ==================== 收尾：清理实验数据 ====================
        print("\n=== 清理 ===")
        async with async_session_factory() as session:
            result = await session.execute(select(User).where(User.name == test_name))
            rows_to_clean = result.scalars().all()
            for row in rows_to_clean:
                await session.delete(row)
            await session.commit()
            show("本次删除的行数", len(rows_to_clean))

    finally:
        await async_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
