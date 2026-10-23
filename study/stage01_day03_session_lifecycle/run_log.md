# 运行记录

## 环境

- 日期：2026-09-15
- 数据库：MySQL 开发库
- SQLAlchemy 版本：TODO（`uv run --project backend python -c "import sqlalchemy; print(sqlalchemy.__version__)"`）

## 执行命令

```powershell
TODO
```

## 实际输出

```text
TODO
```

## 预测 vs 实际

把和预测不一致的条目列在这里，并写一句解释。

| 实验 | 我预测的 | 实际是 | 为什么 |
|---|---|---|---|
| | | | |

## 验证结论

- [ ] 实验 1：`add()` 后 `user.id` 为 `None`
- [ ] 实验 2：`flush()` 后有主键，但另一会话看不到
- [ ] 实验 3：`commit()` 后另一会话能看到
- [ ] 实验 4a：直接 `refresh()` 没有丢掉内存改动
- [ ] 实验 4b：`no_autoflush` + `refresh()` 读到了数据库的值
- [ ] 实验 5a：`rollback()` 后 `user.id` 回到 `None`，库里查不到
- [ ] 实验 5b：`commit` 后 `rollback` 撤销不了数据
- [ ] 收尾：实验数据被完全清理
