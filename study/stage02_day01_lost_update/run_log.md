# 运行记录

## 环境

- 日期：2026-09-17
- 数据库：MySQL 开发库 `exc`（表 `lock_lab_counter`）
- SQLAlchemy 版本：TODO（`uv run --project backend python -c "import sqlalchemy; print(sqlalchemy.__version__)"`）
- Python 版本：TODO（`uv run --project backend python -V`）
- MySQL 版本：TODO（`SELECT VERSION();` —— 隔离级别和锁行为跟版本有关，记一下）

## 执行命令

```powershell
TODO
```

## 实际输出

```text
TODO
```

（把完整的四段实验输出粘进来，尤其是实验 2 的「各 worker 读到的值」。）

## 预测 vs 实际

| 实验 | 我预测的 | 实际是 | 为什么 |
|---|---|---|---|
| 1 顺序 20 次 | | | |
| 2 各 worker 读到的值 | | | |
| 2 并发 20 次 | | | |
| 2 丢失的更新次数 | | | |
| 3 原子 UPDATE | | | |
| 4 asyncio.Lock | | | |

## 多进程验证

```powershell
uv run --project backend python stage02_day01_lost_update/main.py --spawn 2 --only concurrent
```

```text
TODO
```

```powershell
uv run --project backend python stage02_day01_lost_update/main.py --spawn 2 --only locked
```

```text
TODO
```

结论（用自己的话）：TODO

## 验证结论

- [ ] 实验 1：顺序 20 次，最终 `value = 20`
- [ ] 实验 2：各 worker 读到的值大量重复（几乎全是 `0`）
- [ ] 实验 2：并发 20 次后 `value` 远小于 20
- [ ] 实验 3：原子 UPDATE 后 `value = 20`
- [ ] 实验 4：`asyncio.Lock` 后 `value = 20`
- [ ] 实验 4 的耗时明显大于实验 2（进程内锁的代价）
- [ ] 多进程验证：`--spawn 2 --only concurrent` 合计远小于 40
- [ ] 多进程验证：`--spawn 2 --only locked` 合计仍远小于 40
- [ ] 多进程验证：`--only locked` 时每个子进程都报告「丢失 0」——记下这个反差
- [ ] 收尾：计数行被重置，没有残留脏数据
