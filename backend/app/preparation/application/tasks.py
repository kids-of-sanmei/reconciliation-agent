"""任务状态替换函数，使用显式字段参数约束跨阶段更新。"""

from datetime import UTC, datetime

from app.preparation.domain.models import (
    ConfirmationRecord,
    Issue,
    TaskOptions,
    TaskStatus,
    TaskView,
)


def transition(
    task: TaskView,
    status: TaskStatus,
    stage: str,
    *,
    issues: tuple[Issue, ...] = (),
    options: TaskOptions | None = None,
    formula_values: dict[str, str] | None = None,
    confirmations: tuple[ConfirmationRecord, ...] | None = None,
    data_version: int | None = None,
    prepared_ref: str | None = None,
    artifact_names: tuple[str, ...] = (),
) -> TaskView:
    """创建新的不可变任务视图，清理旧版交付引用。

    Args:
        task: 当前持久化视图。
        status: 新阶段状态。
        stage: 面向调用方的实际阶段名称。
        issues: 本阶段问题，默认清空上一阶段临时问题。
        options: 替换配置；None 表示保留。
        formula_values: 替换公式确认结果；None 表示保留。
        confirmations: 完整确认历史；None 表示保留。
        data_version: 新数据版本；None 表示保留。
        prepared_ref: 当前版本已就绪的数据包位置；非发布阶段默认清空。
        artifact_names: 当前允许下载的产物名称。

    Returns:
        时间戳更新、经过构造校验的完整任务对象。
    """
    return TaskView(
        task_id=task.task_id,
        status=status,
        stage=stage,
        created_at=task.created_at,
        updated_at=datetime.now(UTC),
        sources=task.sources,
        options=options if options is not None else task.options,
        formula_values=formula_values if formula_values is not None else task.formula_values,
        confirmations=confirmations if confirmations is not None else task.confirmations,
        data_version=data_version if data_version is not None else task.data_version,
        issues=issues,
        prepared_ref=prepared_ref,
        artifact_names=artifact_names,
    )
