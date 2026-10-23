"""Stage 2 Day 1：并发写同一行会丢数据 —— 复现 lost update。

用法：先读 daily_agent_tasks.md，对每个实验写下预测，再补全 TODO 1～5 运行验证。
预期结果写在 daily_agent_tasks.md 里，先别看，先猜。

今天全部用「读出来 → 在 Python 里加一 → 写回去」这种最容易出错的写法，
目的不是写好代码，而是亲眼看见它是怎么丢数据的。
"""

import argparse
import asyncio
import os
import subprocess
import sys
import time

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from study.stage02_day01_lost_update.models import LabBase, LockLabCounter


DATABASE_URL = "mysql+aiomysql://root:123456@192.168.0.229:3306/exc?charset=utf8mb4"

CONCURRENCY = 20   # 并发协程数
WINDOW = 0.05      # 实验 4 里读和写之间的间隔（秒）
COUNTER_NAME = "day01_lost_update"


TAG = ""  # 跨进程验证时用来区分是哪个子进程在说话


def show(label: str, value: object) -> None:
    print(f"{TAG}  {label}: {value!r}")


def log(message: str = "") -> None:
    print(f"{TAG}{message}")


# ---------------------------------------------------------------------------
# 工具函数（已写好，不用改）
# ---------------------------------------------------------------------------

async def ensure_counter(
    factory: async_sessionmaker[AsyncSession], name: str, reset: bool = True
) -> int:
    """确保实验行存在并返回主键。``reset=True`` 时把 value 重置为 0。

    多进程验证时必须传 ``reset=False``，否则两个进程会互相清零。
    """
    async with factory() as session:
        row = (
            await session.execute(
                select(LockLabCounter).where(LockLabCounter.name == name)
            )
        ).scalar_one_or_none()

        if row is None:
            row = LockLabCounter(name=name, value=0)
            session.add(row)
        elif reset:
            row.value = 0

        await session.commit()
        return row.id


async def read_value(factory: async_sessionmaker[AsyncSession], counter_id: int) -> int:
    async with factory() as session:
        row = await session.get(LockLabCounter, counter_id)
        return row.value


async def warm_up_pool(factory: async_sessionmaker[AsyncSession], n: int) -> None:
    """先把 n 条数据库连接建好。

    不预热的话，建连接（TCP + MySQL 握手）的耗时会混进实验计时里，
    实验 2 和实验 4 的耗时对比就没法看了。这一步只是为了让计时干净。
    """

    async def one() -> None:
        async with factory() as session:
            await session.execute(text("SELECT 1"))

    await asyncio.gather(*[one() for _ in range(n)])


def make_factory() -> tuple:
    """建一个用完即弃的 engine + session factory，给 --spawn 父进程的临时查询用。"""
    engine = create_async_engine(
        DATABASE_URL,
        echo=False,
        pool_size=CONCURRENCY,  # 必须 >= CONCURRENCY，否则协程会排队等连接，竞态被"藏"起来
        max_overflow=0,
    )
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    return engine, factory


async def reset_counter_once() -> int:
    engine, factory = make_factory()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(LabBase.metadata.create_all)
        return await ensure_counter(factory, COUNTER_NAME, reset=True)
    finally:
        await engine.dispose()


async def read_counter_once() -> int:
    engine, factory = make_factory()
    try:
        counter_id = await ensure_counter(factory, COUNTER_NAME, reset=False)
        return await read_value(factory, counter_id)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 主角：一次「读 → 改 → 写」
# ---------------------------------------------------------------------------

async def bump_read_modify_write(
    factory: async_sessionmaker[AsyncSession],
    counter_id: int,
    window: float = 0.0,
    barrier: asyncio.Barrier | None = None,
) -> int | None:
    """一次「读 → 改 → 写」。把读到的值返回，方便看时间线。

    这是并发场景里最经典的错误写法：加一这件事发生在 Python 里，
    数据库完全不知道你要做的是「自增」，它只看到一条 SET value = 常量。
    """
    async with factory() as session:
        # TODO 1: 按主键把计数行读出来（提示：await session.get(LockLabCounter, counter_id)）
        row = None

        current = row.value

        if barrier is not None:
            # 让所有 worker 都「读完」再一起进入「写」的阶段。
            # 真实系统里没有这个 barrier，竞态只是碰运气才发生 —— 那正是这个 bug 难查的原因。
            # 这里手动把它固定下来，好让结论每次都一样。
            await barrier.wait()
        elif window:
            # 模拟真实业务里「读完之后还要算一会儿 / 调个外部接口」
            await asyncio.sleep(window)

        # TODO 2: 在内存里加一，写回对象属性
        # TODO 3: 提交（提示：这是 async 方法）

        return current


async def bump_atomic(
    factory: async_sessionmaker[AsyncSession], counter_id: int
) -> None:
    """让数据库自己完成自增，中间不经过 Python。"""
    async with factory() as session:
        # TODO 4: 写一条原子自增的 UPDATE，交给数据库执行
        #   提示：update(LockLabCounter).where(LockLabCounter.id == counter_id)
        #             .values(value=LockLabCounter.value + 1)
        stmt = None

        await session.execute(stmt)
        await session.commit()


async def bump_with_process_lock(
    factory: async_sessionmaker[AsyncSession],
    counter_id: int,
    lock: asyncio.Lock,
    window: float,
) -> int | None:
    """用进程内的 asyncio.Lock 把整段「读-改-写」保护起来。"""
    # TODO 5: 用 async with lock: 把下面那行包起来
    return await bump_read_modify_write(factory, counter_id, window)


# ---------------------------------------------------------------------------
# 四个实验
# ---------------------------------------------------------------------------

async def experiment_1_sequential(factory, counter_id: int) -> None:
    log(f"\n=== 实验 1：顺序执行 {CONCURRENCY} 次读-改-写（基线）===")
    await ensure_counter(factory, COUNTER_NAME)

    for _ in range(CONCURRENCY):
        await bump_read_modify_write(factory, counter_id)

    show("最终 value", await read_value(factory, counter_id))
    show("预期 value", CONCURRENCY)


async def experiment_2_concurrent(factory, counter_id: int, reset: bool = True) -> None:
    log(f"\n=== 实验 2：{CONCURRENCY} 个协程并发读-改-写 ===")
    if reset:
        await ensure_counter(factory, COUNTER_NAME)
    else:
        log(f"  （不重置，当前起始 value = {await read_value(factory, counter_id)}）")

    await warm_up_pool(factory, CONCURRENCY)
    barrier = asyncio.Barrier(CONCURRENCY)

    started = time.perf_counter()
    reads = await asyncio.gather(
        *[
            bump_read_modify_write(factory, counter_id, barrier=barrier)
            for _ in range(CONCURRENCY)
        ]
    )
    elapsed = time.perf_counter() - started

    value = await read_value(factory, counter_id)
    show("各 worker 读到的值（排序后）", sorted(reads, key=lambda v: (v is None, v)))
    show("最终 value", value)
    show("预期 value", CONCURRENCY)
    show("丢失的更新次数", CONCURRENCY - value)
    show("耗时（秒）", round(elapsed, 2))


async def experiment_3_atomic(factory, counter_id: int) -> None:
    log(f"\n=== 实验 3：{CONCURRENCY} 个协程并发做原子 UPDATE（对照组）===")
    await ensure_counter(factory, COUNTER_NAME)

    started = time.perf_counter()
    await asyncio.gather(
        *[bump_atomic(factory, counter_id) for _ in range(CONCURRENCY)]
    )
    elapsed = time.perf_counter() - started

    value = await read_value(factory, counter_id)
    show("最终 value", value)
    show("预期 value", CONCURRENCY)
    show("丢失的更新次数", CONCURRENCY - value)
    show("耗时（秒）", round(elapsed, 2))


async def experiment_4_asyncio_lock(
    factory, counter_id: int, reset: bool = True, window: float = WINDOW
) -> None:
    log(f"\n=== 实验 4：{CONCURRENCY} 个协程 + 进程内 asyncio.Lock ===")
    if reset:
        await ensure_counter(factory, COUNTER_NAME)
    else:
        log(f"  （不重置，当前起始 value = {await read_value(factory, counter_id)}）")

    await warm_up_pool(factory, CONCURRENCY)
    lock = asyncio.Lock()

    started = time.perf_counter()
    await asyncio.gather(
        *[
            bump_with_process_lock(factory, counter_id, lock, window)
            for _ in range(CONCURRENCY)
        ]
    )
    elapsed = time.perf_counter() - started

    value = await read_value(factory, counter_id)
    show("最终 value", value)
    show("预期 value", CONCURRENCY)
    show("丢失的更新次数", CONCURRENCY - value)
    show("耗时（秒）", round(elapsed, 2))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 2 Day 1：复现 lost update")
    parser.add_argument(
        "--only",
        choices=["all", "concurrent", "locked"],
        default="all",
        help="只跑某一个实验，用于开两个终端做多进程验证："
        "concurrent = 实验 2（不加锁），locked = 实验 4（asyncio.Lock）",
    )
    parser.add_argument(
        "--window",
        type=float,
        default=WINDOW,
        help=f"实验 4 里读和写之间的间隔秒数，默认 {WINDOW}。",
    )
    parser.add_argument(
        "--spawn",
        type=int,
        default=0,
        metavar="N",
        help="【多进程验证】自己拉起 N 个子进程，并把它们的启动时刻对齐。"
        "必须配合 --only concurrent 或 --only locked 使用。",
    )
    parser.add_argument(
        "--start-at",
        type=float,
        default=0.0,
        help="内部参数：等到这个 Unix 时间戳再开始干活，用来对齐多个进程。",
    )
    parser.add_argument("--tag", default="", help="内部参数：日志前缀，用来区分是哪个子进程。")
    args = parser.parse_args()

    global TAG
    TAG = f"[{args.tag}] " if args.tag else ""

    # ---- 多进程验证：父进程只负责对齐启动时刻并拉起子进程 ----------------
    if args.spawn:
        if args.only == "all":
            parser.error("--spawn 必须配合 --only concurrent 或 --only locked 使用")

        # 先把计数清零，再让子进程在同一个未来时刻一起开工
        await reset_counter_once()
        start_at = time.time() + 8  # 留 8 秒给子进程启动（uv run 本身有秒级开销）

        log(f"父进程拉起 {args.spawn} 个子进程，统一在 {time.strftime('%H:%M:%S', time.localtime(start_at))} 开工")
        procs = [
            subprocess.Popen(
                [
                    sys.executable, os.path.abspath(__file__),
                    "--only", args.only,
                    "--start-at", str(start_at),
                    "--window", str(args.window),
                    "--tag", f"P{i + 1}",
                ]
            )
            for i in range(args.spawn)
        ]
        for p in procs:
            p.wait()

        value = await read_counter_once()
        log(f"\n=== 汇总 ===")
        show(f"{args.spawn} 个子进程各做了 {CONCURRENCY} 次自增，合计应为", args.spawn * CONCURRENCY)
        show("实际 value", value)
        show("丢失的更新次数", args.spawn * CONCURRENCY - value)
        return

    engine = create_async_engine(
        DATABASE_URL,
        echo=False,           # 想看每条 SQL 就把这里改成 True
        pool_size=CONCURRENCY,  # 必须 >= CONCURRENCY，否则协程会排队等连接，竞态被"藏"起来
        max_overflow=0,
    )
    factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    try:
        async with engine.begin() as conn:
            await conn.run_sync(LabBase.metadata.create_all)

        # 多进程验证模式下**不能**重置：两个进程会互相清零，结果就失真了。
        counter_id = await ensure_counter(factory, COUNTER_NAME, reset=args.only == "all")

        if args.only != "all":
            # 对齐启动时刻：所有兄弟进程等到同一个时间戳再一起开工，
            # 这样"有没有重叠"就不再取决于 uv run 的启动快慢。
            delay = args.start_at - time.time()
            if delay > 0:
                log(f"连接已就绪，等待 {delay:.1f} 秒与其他进程对齐…")
                await asyncio.sleep(delay)
            log(f"开工于 {time.strftime('%H:%M:%S')}")

        if args.only == "concurrent":
            log("（--only concurrent：多进程验证模式，不重置计数）")
            await experiment_2_concurrent(factory, counter_id, reset=False)
            return

        if args.only == "locked":
            log("（--only locked：多进程验证模式，不重置计数）")
            await experiment_4_asyncio_lock(
                factory, counter_id, reset=False, window=args.window
            )
            return

        await experiment_1_sequential(factory, counter_id)
        await experiment_2_concurrent(factory, counter_id)
        await experiment_3_atomic(factory, counter_id)
        await experiment_4_asyncio_lock(factory, counter_id)

        # 收尾：把本次实验的数据清掉，只留一张空表给 Day 2 用
        await ensure_counter(factory, COUNTER_NAME)
        print("\n=== 清理 ===")
        show("实验行已重置 value", await read_value(factory, counter_id))

    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
