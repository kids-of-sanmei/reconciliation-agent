# Stage 2 Day 1：并发写同一行会丢数据

Stage 1 回答的是「数据什么时候进库、什么时候对别人可见」。今天开始回答下一个问题：

> 两个 worker 同时改同一行，会不会有一个人的改动凭空消失？

答案是**会**。今天不写新功能，只做一件事：**亲手把这个 bug 复现出来**。

## 怎么学

1. 先读 `daily_agent_tasks.md` 的「Learn」，理解丢失更新的时序。
2. 打开 `main.py`，**先不要运行**。逐个实验看代码，把预测填进 `daily_agent_tasks.md` 的预测表。
3. 补全 TODO 1～5，运行脚本。
4. 对照预期结果。重点看实验 2 的「各 worker 读到的值」——那一排 `0` 就是全部答案。
5. 做「多进程验证」。这一步会推翻你对实验 4 的直觉，是明天的引子。

## 运行

```powershell
uv run --project backend python stage02_day01_lost_update/main.py
```

今天**不需要** `PYTHONPATH=backend`：这个脚本是自包含的，只导入同目录的 `models.py`，
不碰 `backend/app` 下的任何模块。

多进程验证（脚本自己拉起子进程并对齐启动时刻，跑两条命令即可）：

```powershell
uv run --project backend python stage02_day01_lost_update/main.py --spawn 2 --only concurrent
uv run --project backend python stage02_day01_lost_update/main.py --spawn 2 --only locked
```

> 为什么要对齐启动时刻：`uv run` 自身有**几秒**启动开销。手动开两个终端的话，两个进程会错开好几秒，
> 错开就没有重叠，没有重叠就没有竞态——你会看到 40 而不是丢失的结果，从而得出错误结论。
> 作者实测过，手动开两个终端时错位可达 5 秒。

## 关于 `models.py`

实验用的 `lock_lab_counter` 表定义在本目录的 `models.py` 里，用了一个**独立的 `LabBase`**，
故意不复用 `backend/app/model/base_model.py`。

理由：实验表不该混进项目 schema，否则后面做 Alembic 迁移时会莫名其妙多出一张表。
脚本跑完只会把计数行重置为 0，**不会 drop 表**——Day 2～Day 5 还要接着用这张表。

## 安全说明

- 脚本只操作 `lock_lab_counter` 这一张表，其中只写 `name = 'day01_lost_update'` 这一行。
- 不会读写 `user` 表，也不会改动项目里的任何模型。
- 仍然只连开发库 `exc`，别连生产库。

## 完成后你应该能回答

1. 用时间线描述一次丢失更新：两个协程各自在第几步做了什么？
2. 为什么顺序执行 20 次没问题，并发 20 次就少了 19 次？
3. 实验 3 的原子 UPDATE 为什么**不需要**加锁？
4. `asyncio.Lock` 为什么能修好实验 2？它的边界在哪里？
5. 多进程验证里，为什么每个进程都报告「丢失 0」，合计却少了一半？
6. 今天这四个实验里，哪种写法应该出现在真实业务代码里？

第 4、5 题做过多进程验证才能答对。答不上第 5 题，今天就还没结束——
那是通往明天的唯一一座桥。
