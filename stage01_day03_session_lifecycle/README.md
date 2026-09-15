# Stage 1 Day 3：Session 生命周期

昨天你已经能用 `AsyncSession` 跑通 CRUD。今天不写新功能，只做一件事：**搞清楚每一步到底在哪一刻生效**。

## 怎么学

1. 先读 `daily_agent_tasks.md` 里的「Learn」表格，理解五个方法在时间线上的位置。
2. 打开 `main.py`，**先不要运行**。逐个实验看代码，把预测填进 `daily_agent_tasks.md` 的预测表。
3. 补全 TODO 1～13，运行脚本（命令见 `daily_agent_tasks.md` 的 Verify）。
4. 对照预期结果，凡是不一致的，去 `notes.md` 里写下你的解释。
5. 实验 4a 是今天唯一一个「反直觉」的点，值得单独多花十分钟。

## 运行

```powershell
$env:PYTHONPATH="backend"
uv run --project backend python stage01_day03_session_lifecycle/main.py
```

想看清楚每条语句什么时候发出去，把 `main.py:24` 的 `echo=False` 改成 `True`，再跑一遍实验 4a 和 5a。

## 安全说明

- 脚本只操作 `name` 以 `lifecycle_` 开头的记录，收尾时会全部删掉。
- 实验 4a / 4b 故意只改内存不提交，退出 `async with` 时自动回滚，不会写坏已有数据。
- 仍然只连开发库，别连生产库。

## 完成后你应该能回答

1. `flush()` 和 `commit()` 的区别是什么？为什么 `flush()` 之后另一个会话还看不到数据？
2. 为什么 `refresh()` 不能用来丢弃未提交的内存改动？`no_autoflush` 做了什么？
3. `rollback()` 为什么能撤销实验 5a 的 INSERT，却撤销不了实验 5b 的？
4. `expire_on_commit=False` 在这里帮我们避免了什么？

答不上第 2 题的话，今天就还没结束 —— 那是今天唯一的新知识点。
