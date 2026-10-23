# Stage 1 Day 2：AsyncSession CRUD

今天只练一个核心：通过 `AsyncSession` 操作 ORM 模型。

请先打开 `main.py`，按 TODO 逐项补全，不要先复制完整答案。完成后执行 `daily_agent_tasks.md` 中的验证命令，并把结果记录到 `run_log.md`。

本练习会删除自己创建的测试用户，但仍建议只连接开发数据库，不要连接生产数据库。

完成后，你应该能回答：

1. 为什么 `create_all()` 使用 Engine/Connection，而日常 CRUD 使用 Session？
2. `session.add(user)` 后为什么还不能认为数据已经永久写入？
3. `select(User).where(User.id == user_id)` 返回结果后，如何取得一个对象？
