# FastAPI + SQLAlchemy 异步数据库学习路线

## 学习目标

- 主题：SQLAlchemy 2.0 异步 ORM，并与 FastAPI 集成。
- 目标：能够独立为当前对账项目设计模型、实现可靠的异步 CRUD API，并能迁移和测试数据库结构。
- 当前水平假设：Python/FastAPI 入门；已经会创建异步引擎，并用 `Base.metadata.create_all()` 创建表。
- 掌握标准：以实践项目能力为主，同时理解连接、会话、事务和迁移的边界。
- 建议节奏：5 周，每周 4 天，每天 45～90 分钟；每次只学习一个核心概念并完成代码验证。

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
- 编写一个独立脚本，对 `User` 表完成新增、查询、修改、删除。
- 比较 `flush`、`commit` 和 `refresh` 的效果。

### 阶段验收

- 不看答案，能在 30 分钟内写出异步单表 CRUD。
- 能解释为什么日常查询使用 `Session`，而不是直接使用 `Engine`。
- 能说明 `commit` 前后数据库数据的差别。

## Stage 2：查询、事务与表关系

### 目标

能够写实际业务查询，并保证一组数据库操作要么全部成功，要么全部失败。

### 核心概念

- `where`、`order_by`、`limit`、`offset`、聚合查询
- 唯一约束、索引、外键
- 一对多关系与 `relationship`
- `commit`、`rollback`、`session.begin()`
- 异步 ORM 的懒加载问题与 `selectinload`

### 练习

- 为用户列表增加条件搜索和分页。
- 创建“对账任务—对账结果”一对多模型。
- 故意制造异常，验证事务回滚。

### 阶段验收

- 能写分页和条件组合查询。
- 能正确建立并查询一对多关系。
- 能通过实验说明事务回滚确实没有留下半成品数据。

## Stage 3：接入 FastAPI

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

## Stage 4：Alembic 数据库迁移

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

## Stage 5：可靠性与测试

### 目标

让数据库代码具备可测试性，并能处理连接、并发和异常场景。

### 核心概念

- 测试数据库和测试隔离
- pytest 异步测试
- 事务型测试夹具
- 连接池基本参数
- N+1 查询、日志与慢查询意识

### 练习

- 为 CRUD 和 API 编写成功/失败测试。
- 验证重复写入、回滚和并发更新场景。
- 根据 SQL 日志识别一次多余查询。

### 阶段验收

- 测试可以重复运行且不会污染业务数据库。
- 核心 CRUD 的正常与异常路径均有测试。
- 能解释连接池大小不是越大越好。

## 最终项目：对账任务持久化

为当前 reconciliation 项目实现数据库持久化：保存对账任务、文件信息、运行状态、汇总结果和错误信息；提供创建任务、查询详情、分页历史和删除记录接口；使用 Alembic 管理结构，并用自动化测试覆盖主要流程。

最终通过标准：从空数据库执行迁移后，API 和测试均可运行；一次任务的状态变更具有事务边界；代码分层明确；README 写清启动与验证命令。
