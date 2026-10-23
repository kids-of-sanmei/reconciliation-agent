## 2026-09-17 Stage 2 Day 1

Stage: Stage 2 — 事务边界与并发锁（分布式异步）  
Goal: 亲手复现「丢失更新（lost update）」，说清并发写覆盖的时序，并分清原子 UPDATE、`asyncio.Lock`、数据库锁各自能解决什么、管不到什么  
Estimated time: 75～90 分钟  
Task directory: `stage02_day01_lost_update/`

### Learn

今天只学一个概念：**丢失更新（lost update）**。

先把 Day 3 的结论接上：`commit` 是可见性的分界线。但「别人看不到我没提交的数据」并不等于
「我的更新不会丢」。看下面这个时序：

```text
时间 →

协程 A：  SELECT value → 0
协程 B：                          SELECT value → 0
协程 A：  UPDATE value = 1  ✓  COMMIT
协程 B：                                              UPDATE value = 1  ✓  COMMIT
                                                      ↑ B 用自己的 0 算出了 1，把 A 的结果盖掉了

最终 value = 1，但两个 worker 各干了一次活，本该是 2。A 的那次更新凭空消失了。
```

关键点有三个：

1. **问题出在「读」和「写」之间**。A 提交之后，B 手里那个 `0` 已经是过期数据，但 B 不知道。
2. **加一这件事发生在 Python 里**。数据库只收到 `SET value = 1`，它没有能力判断这是个自增。
3. **事务边界救不了它**。两个事务各自都是正确的、原子提交的，问题在于它们的**读**发生在彼此的**写**之前。

今天要对比的三种「修法」，正好对应三个不同层次的答案：

| 修法 | 谁在执行「加一」 | 能解决单进程并发吗 | 能解决多进程 / 多容器并发吗 |
|---|---|---|---|
| 原子 UPDATE（`SET value = value + 1`） | 数据库 | 能 | 能 |
| `asyncio.Lock` | Python 进程 | 能 | **不能**（第二个进程看不到这把锁） |
| 数据库行锁（Day 2 才学） | 数据库 | 能 | 能 |

最后一行留到明天。今天的重点是：**先确认问题真的存在，再确认「不是所有并发问题都要靠加锁解决」**。

### Build

补全 `main.py` 里的 TODO 1～5。骨架、实验编排和打印都已经写好，你只需要填 ORM 调用。

**先预测，再运行。** 在下面的表里写下你的预测：

| 实验 | 观察点 | 我的预测 | 实际结果 |
|---|---|---|---|
| 1 | 顺序 20 次后 `value` | | |
| 2 | 各 worker 读到的值 | | |
| 2 | 并发 20 次后 `value` | | |
| 2 | 丢失的更新次数 | | |
| 3 | 原子 UPDATE 后 `value` | | |
| 4 | `asyncio.Lock` 后 `value` | | |

### Verify

今天这个脚本只依赖 `sqlalchemy` / `aiomysql`，**不需要** `PYTHONPATH=backend`（它不导入项目里的模块）：

```powershell
uv run --project backend python stage02_day01_lost_update/main.py
```

**预期实际结果**（先自己猜完再看这一列；右边是作者实测值）：

| 实验 | 观察点 | 预期结果 | 实测 | 原因 |
|---|---|---|---|---|
| 1 | 顺序 20 次后 `value` | `20` | `20` | 没有并发，读到的永远是最新值 |
| 2 | 各 worker 读到的值 | 全是 `0` | 20 个 `0` | 20 个协程在任何一个 commit 之前就都读完了 |
| 2 | 并发 20 次后 `value` | **`1`** | `1` | 20 次自增只剩下最后那一次 |
| 2 | 丢失的更新次数 | **`19`** | `19` | `20 - 1` |
| 2 | 耗时 | 很快 | `0.19` 秒 | 没人在等 |
| 3 | 原子 UPDATE 后 `value` | `20` | `20` | 加一在数据库里完成，InnoDB 用行锁保证 `value + 1` 读到最新值 |
| 3 | 丢失的更新次数 | `0` | `0` | —— |
| 4 | `asyncio.Lock` 后 `value` | `20` | `20` | 20 次「读-改-写」被排成一队 |
| 4 | 耗时 | 明显变慢 | `1.36` 秒 | 约 `20 × 0.05`，串行化的代价 |

实验 4 的耗时是实验 2 的 **7 倍**，这本身就是答案的一部分：**进程内锁是用「串行化」换正确性**的。

> 实验 2 用了一个 `asyncio.Barrier`，让 20 个 worker 全部读完再一起写。
> 真实系统里没有这个 barrier，竞态是**碰运气**才发生的——那正是这个 bug 在生产里难查的原因。
> 这里手动把它固定下来，好让结论每次都一样。

**多进程验证（选做，但强烈建议做，这是明天的引子）**

脚本可以自己拉起子进程，**并把它们的启动时刻对齐**（`uv run` 自身有几秒启动开销，
手动开两个终端会错开，错开就没有竞态了——这一点很重要，别跳过）：

```powershell
uv run --project backend python stage02_day01_lost_update/main.py --spawn 2 --only concurrent
uv run --project backend python stage02_day01_lost_update/main.py --spawn 2 --only locked
```

第一次跑（不加锁），实测 40 次自增只留下 `1`：

```text
=== 汇总 ===
  2 个子进程各做了 20 次自增，合计应为: 40
  实际 value: 1
  丢失的更新次数: 39
```

第二次跑（`asyncio.Lock`）才是今天的重头戏，实测：

```text
[P1]   最终 value: 20      丢失的更新次数: 0
[P2]   最终 value: 20      丢失的更新次数: 0
=== 汇总 ===
  2 个子进程各做了 20 次自增，合计应为: 40
  实际 value: 20
  丢失的更新次数: 20
```

**每个进程都报告「我这边 20 次，一次没丢」，但合计只有 20。** 一半的活儿凭空消失了，
而没有任何一方觉得出了问题。每个进程各有一把 `asyncio.Lock`，它们互相看不见对方。
这正是明天要用**数据库行锁**的原因——只有数据库是两边都能看见的。

### Deliverable

- 补全的 `main.py`
- 填写后的 `notes.md`（用自己的话，不要抄表格）
- `run_log.md` 里粘贴实际命令与输出
- 上面那张预测表填满

### Completion Criteria

- 脚本能完整跑完，不报错；实验 1、3、4 的 `value` 都是 20。
- 能**用时间线**描述一次丢失更新是怎么发生的：哪个协程在哪一步覆盖了谁。
- 能解释实验 3 的原子 UPDATE 为什么不需要加任何锁。
- 能说出 `asyncio.Lock` 的边界在哪里，并能解释多进程验证里那个现象：
  **每个进程都报告「丢失 0」，但合计少了一半。**
- 能回答：**今天这四个实验里，哪一种写法是应该出现在真实业务代码里的？**

### If Stuck

- 报 `AttributeError: 'NoneType' object has no attribute 'value'`：TODO 1 还没填。
- 实验 2 的结果居然是 `20`：确认 `pool_size` 没被改小——池子小于 `CONCURRENCY` 时协程会排队等连接，
  竞态就被"藏"起来了，`asyncio.Barrier` 甚至会直接卡住等不齐人。这本身就是个值得记下来的现象。
- `--spawn` 跑起来卡住不动：`Barrier` 要求 20 个 worker 全部到齐，只要有一个协程拿不到连接就会一直等。
  把 `CONCURRENCY` 调小，或确认 `pool_size >= CONCURRENCY`。
- 报 `QueuePool limit of size 20 overflow 10 reached`：把 `CONCURRENCY` 调小，或把 `pool_size` 调大。
- 报 `MissingGreenlet`：你在 `await` 之外访问了一个已经过期（expired）的属性。检查是不是在读 `commit()` 之后的 `row.value`。
- 报 `Table 'exc.lock_lab_counter' doesn't exist`：`create_all` 那段没执行到，检查 `engine.begin()` 那几行。
- 想看每一步发了什么 SQL：把 `main.py` 里 `create_async_engine` 的 `echo` 改成 `True`。
  实验 2 里你会看到 20 条 `SELECT` 全部挤在一起，然后才是 20 条 `UPDATE`。
- 实验 2 的 `value` 是 `2` 而不是 `1`：正常，事件循环调度有抖动，不影响结论。
