## 2026-09-14 Stage 1 Day 2

Stage: Stage 1 — ORM 基础闭环  
Goal: 使用 `AsyncSession` 完成 `User` 表的新增、查询、修改、删除  
Estimated time: 60～90 分钟  
Task directory: `stage01_day02_async_session_crud/`

### Learn

- 理解 `Engine` 管理数据库连接，`Session` 管理一次业务操作中的 ORM 对象和事务。
- 理解 `async_sessionmaker(engine)` 是会话工厂，每次调用会创建一个独立 `AsyncSession`。
- 认识 `session.add()`、`await session.commit()`、`await session.refresh()` 和 `select()`。

### Build

在 `main.py` 中补全 TODO，按以下顺序操作：

1. 创建异步引擎和 `async_sessionmaker`。
2. 新增一个唯一名字的用户并提交。
3. 根据用户 ID 查询该用户。
4. 修改角色并提交。
5. 删除用户并提交。
6. 再查一次，确认结果为 `None`。

### Verify

从仓库根目录运行：

```powershell
$env:PYTHONPATH="backend"
uv run --project backend python stage01_day02_async_session_crud/main.py
```

预期最后看到：

```text
删除后查询结果: None
CRUD 练习通过
```

### Deliverable

- 补全的 `main.py`
- 填写后的 `notes.md`
- 将实际命令和输出粘贴到 `run_log.md`

### Completion Criteria

- 脚本连续运行两次都成功，不因固定用户名产生重复数据错误。
- 控制台能看到新增、查询、修改、删除四步结果。
- 能口头解释 `Engine` 和 `Session` 的职责区别。
- 能解释为什么写操作最后需要 `commit()`。

### If Stuck

- 报 `ModuleNotFoundError: app`：确认在仓库根目录设置了 `$env:PYTHONPATH="backend"`。
- 报连接失败：检查 MySQL 地址、端口、账号、数据库名，并确认数据库已启动。
- 查询不到模型表：确认已经运行项目的建表逻辑，或在练习脚本中临时调用 `Base.metadata.create_all()`。
- 不确定查询结果怎么取：先打印 `result`，再尝试 `result.scalar_one_or_none()`。
