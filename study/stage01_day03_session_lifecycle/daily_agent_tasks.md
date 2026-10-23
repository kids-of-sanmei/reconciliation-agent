## 2026-09-15 Stage 1 Day 3

Stage: Stage 1 — ORM 基础闭环  
Goal: 搞清 `add`、`flush`、`commit`、`refresh`、`rollback` 各自在什么时刻做什么，能说清楚「数据什么时候真正进库、什么时候对别人可见」  
Estimated time: 75～90 分钟  
Task directory: `stage01_day03_session_lifecycle/`

### Learn

今天只学一个概念：**Session 的生命周期与事务边界**。五个方法其实是同一条时间线上的五个刻度：

| 方法 | 它做什么 | 有没有 SQL 发往数据库 | 事务结束了吗 |
|---|---|---|---|
| `add()` | 把对象放进 Session 的待写队列 | 没有 | 没有 |
| `flush()` | 把待写的 INSERT/UPDATE/DELETE 发出去 | 有 | 没有 |
| `commit()` | 提交事务 | 有（提交本身） | 结束了 |
| `rollback()` | 回滚当前事务 | 有（回滚） | 结束了 |
| `refresh()` | 重新 SELECT，覆盖对象内存状态 | 有（先 autoflush，再 SELECT） | 没有 |

两个关键判断，今天结束时必须能自己答出来：

1. **「SQL 发出去了」和「数据能被别人看到」是两件事** —— 分界线是 `commit`，不是 `flush`。
2. **`refresh()` 不是纯读取** —— 它在 SELECT 之前会先 autoflush，所以它救不了你未提交的内存改动。

### Build

补全 `main.py` 里的 TODO 1～13。脚本已经搭好骨架和打印语句，你只需要填 ORM 调用。

**先预测，再运行。** 每个实验运行前，在下面的表里写下你猜的结果：

| 实验 | 观察点 | 我的预测 | 实际结果 |
|---|---|---|---|
| 1 | `add()` 后 `user.id` | | |
| 1 | `add()` 后 `user in session.new` | | |
| 2 | `flush()` 后 `user.id` | | |
| 2 | 另一会话能看到未提交的数据吗 | | |
| 3 | 另一会话能看到已提交的数据吗 | | |
| 4a | 改内存后 `role` | | |
| 4a | 直接 `refresh()` 后 `role` | | |
| 4b | `no_autoflush` + `refresh()` 后 `role` | | |
| 5a | `rollback()` 后 `user.id` | | |
| 5a | 回滚后数据库里还能查到吗 | | |
| 5b | `commit` 后再 `rollback`，数据还在吗 | | |

### Verify

从仓库根目录运行（注意 `PYTHONPATH` 指向 `backend`，所以导入写 `app.model.user`）：

```powershell
$env:PYTHONPATH="backend"
uv run --project backend python stage01_day03_session_lifecycle/main.py
```

**预期实际结果**（先自己猜完再看这一列）：

| 实验 | 观察点 | 预期结果 | 原因 |
|---|---|---|---|
| 1 | `add()` 后 `user.id` | `None` | 还没发 INSERT，数据库没分配主键 |
| 1 | `user in session.new` | `True` | 对象在待写队列里 |
| 2 | `flush()` 后 `user.id` | 真实主键（非 None） | INSERT 已发出，主键已回填 |
| 2 | 另一会话能看到吗 | `False` | 事务未提交，禁止脏读 |
| 3 | 另一会话能看到吗 | `True` | 已提交，别人可见了 |
| 3 | 另一会话读到的 `role` | `'admin'` | —— |
| 4a | 改内存后 `role` | `'in_memory_only'` | 只改了内存 |
| 4a | 直接 `refresh()` 后 `role` | **还是 `'in_memory_only'`** | refresh 先 autoflush，把改动写进事务，再 SELECT 读回来 |
| 4b | `no_autoflush` + `refresh()` 后 | **`'admin'`** | 关掉 autoflush 后，SELECT 读到的才是数据库里的真实值 |
| 5a | `flush()` 后 `user.id` | 非 None | INSERT 已发出 |
| 5a | `rollback()` 后 `user.id` | `None` | 对象被踢出 Session，回到 transient |
| 5a | 数据库里还能查到吗 | `False` | 事务回滚，INSERT 被撤销 |
| 5b | `commit` 后再 `rollback`，数据还在吗 | `True` | 提交是分界线，已提交的数据无法用 rollback 撤销 |
| 清理 | 本次删除的行数 | `2`（只做 5a 则 `1`） | 只有实验 3 和 5b 真的提交过 |

> 实验 4a 的结果和直觉相反，这是今天的重点。看完结果后打开 `echo=True` 再跑一次 4a，你会看到 `refresh()` 触发了 UPDATE 和 SELECT 两条 SQL —— 这就是原因。

### Deliverable

- 补全的 `main.py`
- 填写后的 `notes.md`（用自己的话，不要抄表格）
- `run_log.md` 里粘贴实际命令与输出
- 上面那张预测表填满

### Completion Criteria

- 脚本能完整跑完，不报错，最后清理掉本次实验产生的数据。
- 能不看资料回答：`flush` 和 `commit` 的区别是什么？
- 能解释实验 4a 为什么 `refresh()` 没有丢掉内存里的改动，以及 `no_autoflush` 为什么能解决。
- 能解释为什么 `rollback()` 能撤销实验 5a 的 INSERT，却撤销不了实验 5b 的 INSERT。
- 能说出 `expire_on_commit=False` 在这里避免了什么麻烦。

### If Stuck

- 报 `ModuleNotFoundError: No module named 'backend'`：导入要写 `from app.model.user import User`，不是 `backend.app.model.user`（`PYTHONPATH` 已经指向 `backend/` 了，别再带一层）。
- 报 `MissingGreenlet`：说明你在 `await` 之外触发了懒加载。检查是不是在 `rollback()` 之后又去读对象属性 —— 那会触发重新 SELECT。
- 实验 2 里另一会话居然看到了数据：先确认那不是一个已经存在的事务，`async with` 新开的 session 才是全新事务。
- 不知道某个方法的调用形式：今天全部是 `await session.xxx(...)`，只有 `session.add()` 不带 `await`。
- 想直接看 SQL：把 `create_async_engine` 的 `echo` 改成 `True`。
