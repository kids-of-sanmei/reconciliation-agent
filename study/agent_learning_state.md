# 学习状态

- 当前主题：SQLAlchemy 2.0 异步 ORM → 分布式/异步场景下的并发锁 → Redis
- 当前阶段：Stage 2 — 事务边界与并发锁（分布式异步）
- 当前周：Week 1
- 上次完成任务：Stage 1 Day 3 — Session 生命周期（`flush` / `commit` / `refresh` / `rollback`），用户于 2026-09-17 确认完成
- 今日分配任务：Stage 2 Day 1 — 复现丢失更新（lost update）与事务边界（任务目录 `stage02_day01_lost_update/`）
- 阻碍：暂无
- 下一建议任务：Stage 2 Day 2 — 悲观锁 `with_for_update()`：行锁范围、`nowait`、锁等待超时

## 被跳过的内容（用户于 2026-09-17 明确要求跳转）

- Stage 1 阶段验收（`stage01_final_acceptance/`）**未做**，目录也尚未创建。用户选择直接进入并发锁专题。
  该项记录为「顺延」，不视为通过；如用户回补，通过后再在状态里标记 Stage 1 关闭。
- Stage 2～3（并发锁 + Redis）已插到原路线前面，原 Stage 2～5 顺延为 Stage 4～7，详见 `agent_developer_roadmap.md` 的路线总览。

## 待回填的记录（进入 Stage 2 验收前必须补齐）

- `stage01_day02_async_session_crud/notes.md` 与 `run_log.md`：仍是 TODO 占位；`main.py` 末尾保留着 `raise NotImplementedError`。
- `stage01_day03_session_lifecycle/notes.md` 与 `run_log.md`：仍是 TODO 占位。用户口头确认 Day 3 已完成，
  但笔记与运行记录没有落盘，Stage 2 的验收会缺少可对照的基线记录。
- 处理方式：不阻塞 Stage 2 的学习，但建议每个 Stage 收尾时一次性补完，别拖到最后。

## 给后续 Codex 的备注

- 用户使用 FastAPI、SQLAlchemy 2.0.52、MySQL 与 aiomysql；处于初学阶段，每日只引入一个核心概念。
- 运行练习脚本的正确方式：仓库根目录下 `uv run --project backend python <脚本路径>`。
  需要导入 `backend/app` 下的模块时，额外设 `$env:PYTHONPATH="backend"`。
- 导入必须写 `from app.model.user import User`。Day 2 的 `main.py` 写成了 `from backend.app.model.user import User`，
  只有把仓库根目录也放进 `PYTHONPATH` 才能运行；Day 3 已改用正确写法，不要回退。
- 开发库连接串（来自既有练习脚本）：`mysql+aiomysql://root:123456@192.168.0.229:3306/exc?charset=utf8mb4`。
- 已在 Day 3 验证过的库行为：`Session.refresh()` 会先 `_autoflush()`（SQLAlchemy 2.0.52 `orm/session.py` 第 3163 行），
  因此「改内存 → refresh」不会丢弃改动，必须配 `session.no_autoflush`。讲 refresh 时按这个事实讲。
- Stage 2 的实验表 `lock_lab_counter` 定义在 `stage02_day01_lost_update/models.py`，用**独立的 `LabBase`**，
  故意不复用 `backend/app/model/base_model.py`，避免实验表混进项目 schema。后续 Day 2～5 沿用这张表；
  该表已于 2026-09-17 在开发库 `exc` 中创建。

## Stage 2 Day 1 的实测基线（2026-09-17 作者验证，Day 2 出问题时拿它对照）

- 实验 1（顺序 20 次）= `20`；实验 2（并发 20 次）= `1`，丢 19；实验 3（原子 UPDATE）= `20`；
  实验 4（`asyncio.Lock`）= `20`，耗时 `1.36` 秒（实验 2 只要 `0.19` 秒）。
- 实验 2 用 `asyncio.Barrier(CONCURRENCY)` 让 20 个 worker 全部读完再一起写，结果才稳定为 `1`。
  **不写 barrier 的话结果是 `2`（8 个读到 0、12 个读到 1）且每次不一样** —— 别为了"更真实"去掉它，
  去掉之后结论就不再可复现了。
- `pool_size` 必须 `>= CONCURRENCY`（脚本里是 20，`max_overflow=0`）。池子小了协程会排队等连接，
  竞态被"藏"起来，而且 `Barrier` 会直接卡死等人到齐。
- 跨进程验证用 `--spawn N --only {concurrent,locked}`：脚本用 `--start-at` 把子进程的开工时刻对齐。
  **手动开两个终端是不行的**：`uv run` 自身有约 5 秒启动开销，两个进程会错开，错开就没有重叠，
  实测会得到 40（看起来"没问题"）——这是个错误结论。
- 跨进程实测：`--spawn 2 --only concurrent` → 40 次自增只剩 `1`；
  `--spawn 2 --only locked` → 只剩 `20`，且**两个子进程各自都报告"丢失 0"**。
  这个反差是 Day 2 最有力的引子，讲数据库行锁时从它切入。
- 开发库里 `lock_lab_counter` 表在每次完整跑完后会被重置为 `value=0`，不会残留脏数据。
