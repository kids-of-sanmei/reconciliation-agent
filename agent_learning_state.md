# 学习状态

- 当前主题：FastAPI + SQLAlchemy 2.0 异步数据库
- 当前阶段：Stage 1 — ORM 基础闭环
- 当前周：Week 1
- 上次完成任务：Stage 1 Day 2 — 使用 `AsyncSession` 完成单表 CRUD（含聚合查询与分页查询，用户已确认掌握）
- 今日分配任务：Stage 1 Day 3 — 比较 `flush`、`commit`、`refresh` 与 `rollback`（任务目录 `stage01_day03_session_lifecycle/`）
- 阻碍：暂无
- 下一建议任务：Stage 1 阶段验收 — 不看答案写出异步单表 CRUD，并解释 `Engine` / `Session` / 事务边界（任务目录 `stage01_final_acceptance/`）
- 给后续 Codex 的备注：
  - 用户使用 FastAPI、SQLAlchemy 2.0.52、MySQL 与 aiomysql；当前是初学阶段，每日只引入一个核心概念。
  - 运行练习脚本的正确方式：仓库根目录下 `$env:PYTHONPATH="backend"`，然后 `uv run --project backend python <脚本路径>`。
  - 导入必须写 `from app.model.user import User`。Day 2 的 `main.py` 写成了 `from backend.app.model.user import User`，只有把仓库根目录也放进 `PYTHONPATH` 才能运行；Day 3 已改用正确写法，不要回退。
  - Day 2 的收尾文件尚未填写：`stage01_day02_async_session_crud/notes.md` 与 `run_log.md` 仍是 TODO 占位，且 `main.py` 末尾保留着 `raise NotImplementedError`。Day 3 任务里已包含回填提示，若用户直接进入阶段验收，需要先补这两份记录。
  - 已在 Day 3 验证过的库行为：`Session.refresh()` 会先 `_autoflush()`（SQLAlchemy 2.0.52 `orm/session.py` 第 3163 行），因此「改内存 → refresh」不会丢弃改动，必须配 `session.no_autoflush`。讲 refresh 时按这个事实讲，不要说成「refresh 会丢弃未提交改动」。
