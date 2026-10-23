# 每日任务索引（仅追加）

## 2026-09-14 Stage 1 Day 2

- 阶段：Stage 1 — ORM 基础闭环
- 目标：使用 `async_sessionmaker` 和 `AsyncSession` 对 `User` 表完成一次完整 CRUD
- 任务目录：`stage01_day02_async_session_crud/`
- 任务文件：`stage01_day02_async_session_crud/daily_agent_tasks.md`
- 预计时间：60～90 分钟
- 下一步：比较 `flush`、`commit`、`refresh` 与 `rollback`

## 2026-09-15 Stage 1 Day 3

- 阶段：Stage 1 — ORM 基础闭环
- 目标：搞清 `add`、`flush`、`commit`、`refresh`、`rollback` 各自在什么时刻做什么，能说清「数据什么时候真正进库、什么时候对别人可见」
- 任务目录：`stage01_day03_session_lifecycle/`
- 任务文件：`stage01_day03_session_lifecycle/daily_agent_tasks.md`
- 预计时间：75～90 分钟
- 下一步：Stage 1 阶段验收（不看答案写出异步单表 CRUD）

## 2026-09-17 Stage 2 Day 1

- 阶段：Stage 2 — 事务边界与并发锁（分布式异步）
- 目标：亲手复现「丢失更新（lost update）」，说清事务边界与并发写覆盖的时序，并分清原子 UPDATE、`asyncio.Lock`、数据库锁各自能解决什么、管不到什么
- 任务目录：`stage02_day01_lost_update/`
- 任务文件：`stage02_day01_lost_update/daily_agent_tasks.md`
- 预计时间：75～90 分钟
- 备注：用户于本日选择从 Stage 1 跳到并发锁专题；Stage 1 阶段验收记录为「顺延」，未创建目录
- 下一步：Stage 2 Day 2 — 悲观锁 `with_for_update()`：行锁范围、`nowait`、锁等待超时
