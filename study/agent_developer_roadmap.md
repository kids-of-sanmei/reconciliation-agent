# FastAPI + SQLAlchemy 异步数据库学习路线

## 学习目标

- 主题：SQLAlchemy 2.0 异步 ORM 与 FastAPI 集成；在此基础上掌握**分布式/异步场景下的并发控制**与 **Redis 协作**。
- 目标：能够独立为当前对账项目设计模型、实现可靠的异步 CRUD API，能在多 worker 并发下保证数据正确性，并能用 Redis 解决跨进程的互斥与缓存问题。
- 当前水平假设：Python/FastAPI 入门；已经会创建异步引擎、用 `Base.metadata.create_all()` 建表，能用 `AsyncSession` 完成单表 CRUD 并解释 `flush`/`commit`/`rollback` 的区别。
- 掌握标准：以实践项目能力为主，同时理解连接、会话、事务、锁和缓存的边界。
- 建议节奏：7 个阶段，每周 4 天，每天 45～120 分钟；每次只学习一个核心概念并完成代码验证。

## 路线总览

| 阶段 | 主题 | 建议天数 | 状态 |
|---|---|---|---|
| Stage 1 | ORM 基础闭环 | 3 | 进行中（Day 1～3 已完成，阶段验收待补） |
| Stage 2 | 事务边界与并发锁（分布式异步） | 5 | 当前阶段 |
| Stage 3 | Redis 分布式协作 | 5 | 未开始 |
| Stage 4 | 查询、关系与高效加载 | 4 | 未开始 |
| Stage 5 | 接入 FastAPI | 4 | 未开始 |
| Stage 6 | Alembic 数据库迁移 | 3 | 未开始 |
| Stage 7 | 可靠性与测试 | 4 | 未开始 |
| 最终项目 | 对账任务持久化（含并发安全） | —— | 未开始 |

> 说明：原路线图的 Stage 2～5（查询与事务 / FastAPI / Alembic / 可靠性与测试）顺延为 Stage 4～7。
> 原因是用户在 2026-09-17 明确要求优先学习「分布式异步场景下的 SQLAlchemy 锁」，随后学习 Redis。
> 原 Stage 2 中的「事务」部分已并入 Stage 2 一并学习，剩余查询与关系内容保留在 Stage 4。

---

## Stage 1：ORM 基础闭环

### 目标

理解 `Engine`、`AsyncSession`、ORM 模型和事务各自负责什么，能独立完成单表 CRUD。

### 核心概念

- `DeclarativeBase`、`Mapped`、`mapped_column`
- `async_sessionmaker` 与 `AsyncSession`
- `add`、`flush`、`commit`、`refresh`
- `select`、`update`、`delete`
- `scalar_one_or_none` 和 `scalars`

### 练习

- 已完成：创建异步引擎并通过 `Base.metadata.create_all()` 建表。
- 已完成：编写独立脚本，对 `User` 表完成新增、查询、修改、删除。
- 已完成：比较 `flush`、`commit`、`refresh` 与 `rollback` 的效果。

### 阶段验收

- 不看答案，能在 30 分钟内写出异步单表 CRUD。
- 能解释为什么日常查询使用 `Session`，而不是直接使用 `Engine`。
- 能说明 `commit` 前后数据库数据的差别。

> 阶段验收目录 `stage01_final_acceptance/` 尚未创建。用户已确认 Day 3 完成并选择直接进入并发锁专题，
> 该项记录为「顺延」，可随时回补。

---

## Stage 2：事务边界与并发锁（分布式异步）★ 当前阶段

### 目标

能复现并解释「并发更新丢失」，并能**根据场景选择合适的并发控制手段**——原子 UPDATE、悲观锁、乐观锁、
队列抢占、advisory lock——而不是无脑加锁。同时能说清「进程内锁」和「跨进程锁」的边界。

### 核心概念

- 事务边界：`autobegin`、`async with session.begin()`、提交与可见性的关系
- MySQL InnoDB 隔离级别；快照读与当前读的区别
- 丢失更新（lost update）的时序
- 原子 UPDATE：`SET value = value + 1`（很多时候根本不需要锁）
- 悲观锁：`with_for_update()`、`with_for_update(read=True)`、`nowait`、`skip_locked`
- 多 worker 抢单模式：`SELECT ... FOR UPDATE SKIP LOCKED`
- 乐观锁：`__mapper_args__ = {"version_id_col": ...}`、`StaleDataError`、冲突重试
- 死锁的成因与规避：统一加锁顺序、缩短事务、异常重试
- 锁的代价：锁持有时间 = 连接占用时间，直接关系到连接池大小
- 进程内锁 vs 跨进程锁：`asyncio.Lock` 管不住第二个进程
- advisory lock：MySQL `GET_LOCK()` / PostgreSQL `pg_advisory_lock()`

### 练习（每天一个）

- Day 1：复现丢失更新，并对比原子 UPDATE 与 `asyncio.Lock`（任务目录 `stage02_day01_lost_update/`）
- Day 2：用 `with_for_update()` 修好它，观察行锁范围、`nowait` 与锁等待超时
- Day 3：用 `SKIP LOCKED` 实现多 worker 抢占对账任务
- Day 4：用 version 列实现乐观锁，处理 `StaleDataError` 并写重试
- Day 5：制造一次死锁并修复；比较各方案的适用场景与代价

### 阶段验收（`stage02_final_acceptance/`）

- 从零写出一个「多 worker 安全抢占 + 更新状态」的对账任务调度脚本，并能用并发压测证明不多不少。
- 能说明每种锁方案分别适合什么场景、代价是什么。
- 能解释为什么锁必须配合短事务，以及锁与连接池的相互影响。
- 能回答：什么时候**不需要**加锁？

---

## Stage 3：Redis 分布式协作

### 目标

能用 `redis.asyncio` 实现跨进程的分布式锁与缓存，并清楚 Redis 锁的失效边界——知道它在什么情况下会
失效，而不是把它当成万能方案。

### 核心概念

- `redis.asyncio` 连接与连接池；与数据库连接池的异同
- 字符串操作、TTL、过期语义
- `SET key value NX PX` 实现分布式锁
- 为什么释放锁必须用 Lua 校验持有者（`GET` 再 `DEL` 不是原子操作）
- 锁续期（watchdog）、业务超时、锁误删
- Redis 锁 vs 数据库锁：什么时候用哪个
- 缓存模式 cache-aside；缓存穿透、击穿、雪崩
- 单点 Redis 的可靠性边界与 Redlock 争议

### 练习（每天一个）

- Day 1：连接 Redis，做基本 KV、TTL 与过期实验
- Day 2：手写 `SET NX PX` 锁，并写出安全释放的 Lua 脚本
- Day 3：实现锁续期，构造「业务超时导致锁提前过期」的失效场景
- Day 4：给对账结果加一层 cache-aside 缓存，并复现一次缓存击穿
- Day 5：把 Stage 2 的任务抢占从数据库锁换成 Redis 锁，对比两者优劣

### 阶段验收（`stage03_final_acceptance/`）

- 能写出一个可重入性、持有者校验、超时续期都考虑到的 Redis 锁工具类。
- 能构造并解释至少一种 Redis 锁失效场景。
- 能给出「这个场景该用 DB 锁还是 Redis 锁」的判断依据，并说明理由。

---

## Stage 4：查询、关系与高效加载

### 目标

能够写实际业务查询，并正确处理表关系与加载策略。

> 原 Stage 2 的事务部分已并入 Stage 2 学习，本阶段只保留查询与关系内容。

### 核心概念

- `where`、`order_by`、`limit`、`offset`、聚合查询
- 唯一约束、索引、外键
- 一对多关系与 `relationship`
- 异步 ORM 的懒加载问题与 `selectinload`
- `session.begin()` 与嵌套事务 `begin_nested()`

### 练习

- 为用户列表增加条件搜索和分页。
- 创建「对账任务—对账结果」一对多模型。
- 用 `selectinload` 消除一次 N+1 查询。

### 阶段验收

- 能写分页和条件组合查询。
- 能正确建立并查询一对多关系。
- 能通过 SQL 日志证明 N+1 已被消除。

---

## Stage 5：接入 FastAPI

### 目标

建立清晰的 `model → schema → CRUD/service → router` 调用链。

### 核心概念

- Pydantic 请求与响应模型
- FastAPI `Depends` 注入数据库会话
- 每个请求一个 Session
- HTTP 状态码与异常转换
- 返回 ORM 对象时的序列化

### 练习

- 实现用户的 POST、GET、PATCH、DELETE API。
- 为不存在、重复数据等情况返回合理错误。
- 用 Swagger 或 HTTP 客户端手动验证接口。

### 阶段验收

- API 能完成完整 CRUD，响应结构和状态码合理。
- 请求结束后 Session 会正确关闭。
- 路由层不堆积数据库细节。

---

## Stage 6：Alembic 数据库迁移

### 目标

停止依赖 `create_all()` 修改正式数据库结构，掌握可追踪、可回滚的迁移。

### 核心概念

- 初始化 Alembic
- `target_metadata`
- 自动生成与人工审查迁移
- `upgrade`、`downgrade`
- 数据迁移与结构迁移的区别

### 练习

- 为 `User` 新增字段并生成迁移。
- 执行升级、降级，再次升级。
- 检查生成 SQL 是否符合预期。

### 阶段验收

- 能从空数据库迁移到最新版本。
- 能回滚一次迁移而不手工改表。
- 能解释为什么 `create_all()` 不能代替迁移工具。

---

## Stage 7：可靠性与测试

### 目标

让数据库代码具备可测试性，并能处理连接、并发和异常场景。

### 核心概念

- 测试数据库和测试隔离
- pytest 异步测试
- 事务型测试夹具
- 连接池基本参数（`pool_size`、`max_overflow`、`pool_timeout`）
- 并发测试：如何稳定复现竞态
- N+1 查询、日志与慢查询意识

### 练习

- 为 CRUD 和 API 编写成功/失败测试。
- 为 Stage 2 的并发抢占脚本补一个「不多不少」的并发测试。
- 根据 SQL 日志识别一次多余查询。

### 阶段验收

- 测试可以重复运行且不会污染业务数据库。
- 核心 CRUD 的正常与异常路径均有测试。
- 能解释连接池大小不是越大越好，以及它和锁持有时间的关系。

---

## 最终项目：对账任务持久化（含并发安全）

为当前 reconciliation 项目实现数据库持久化：保存对账任务、文件信息、运行状态、汇总结果和错误信息；
提供创建任务、查询详情、分页历史和删除记录接口；使用 Alembic 管理结构，并用自动化测试覆盖主要流程。

并发要求（Stage 2 / Stage 3 的产出）：

- 多个 worker 同时启动时，一个对账任务只能被一个 worker 领取（抢占不重复、不遗漏）。
- 任务状态更新使用乐观锁或行锁保护，并发更新不会互相覆盖。
- 能说明该场景为什么选择数据库锁或 Redis 锁，并给出理由。

最终通过标准：从空数据库执行迁移后，API 和测试均可运行；一次任务的状态变更具有事务边界；
多 worker 并发压测下任务不重复执行；代码分层明确；README 写清启动与验证命令。
