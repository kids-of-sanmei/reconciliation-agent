"""图节点保存文件引用，避免把整张工作簿塞入模型或检查点。"""

import logging
from collections.abc import Callable
from typing import Literal, Protocol, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from app.preparation.application.steps import PreparationSteps
from app.preparation.application.tasks import transition
from app.preparation.domain.errors import PreparationError
from app.preparation.domain.models import Confirmation, Issue, TaskStatus

logger = logging.getLogger(__name__)


class PreparationState(TypedDict):
    """检查点状态只引用任务及数据版本，明细由任务仓库管理。"""

    task_id: str
    data_version: int


class PreparationNode(Protocol):
    """保留命名参数约定，满足 LangGraph 节点协议。"""

    def __call__(self, state: PreparationState) -> PreparationState:
        """执行节点。

        Args:
            state: 检查点中的任务引用。

        Returns:
            更新后的任务版本引用。
        """
        ...


class PreparationGraph:
    """将类型化业务用例连接为可中断、可恢复的图。"""

    def __init__(self, steps: PreparationSteps) -> None:
        """保存节点依赖。

        Args:
            steps: 文件准备、识别及确认的业务用例。
        """
        self.steps = steps

    def build(
        self, checkpointer: SqliteSaver
    ) -> CompiledStateGraph[PreparationState, None, PreparationState, PreparationState]:
        """编译持久化图。

        Args:
            checkpointer: 生命周期由服务层管理的 SQLite 检查点存储。

        Returns:
            与存储连接同生命周期的可执行状态图。
        """
        builder = StateGraph(PreparationState)
        builder.add_node("parse", self._guard(self.steps.parse))
        builder.add_node("identify", self._guard(self.steps.identify))
        builder.add_node("confirm", self._confirm)
        builder.add_node("prepare", self._guard(self.steps.prepare))
        builder.add_edge(START, "parse")
        builder.add_conditional_edges(
            "parse", self._after_parse, {"identify": "identify", "end": END}
        )
        builder.add_conditional_edges(
            "identify",
            self._after_identify,
            {"prepare": "prepare", "confirm": "confirm", "end": END},
        )
        builder.add_edge("confirm", "identify")
        builder.add_conditional_edges(
            "prepare", self._after_prepare, {"confirm": "confirm", "end": END}
        )
        return builder.compile(checkpointer=checkpointer)

    def _guard(self, operation: Callable[[str], None]) -> PreparationNode:
        """把可预期或工具执行错误持久化为任务失败。

        Args:
            operation: 只接受任务标识的单阶段用例。

        Returns:
            不吞掉 LangGraph 中断的普通节点函数。
        """

        def node(state: PreparationState) -> PreparationState:
            """执行一个阶段，返回对应版本引用。

            Args:
                state: 当前任务引用。

            Returns:
                最新数据版本；错误细节写入任务问题。
            """
            try:
                operation(state["task_id"])
            except Exception as exc:
                logger.exception("Preparation stage failed: %s", operation.__name__)
                code = exc.code if isinstance(exc, PreparationError) else "PREPARATION_TOOL_FAILED"
                message = (
                    exc.message
                    if isinstance(exc, PreparationError)
                    else "处理工具失败，请检查后端日志并重试"
                )
                task = self.steps.repository.get_task(state["task_id"])
                self.steps.repository.save_task(
                    transition(
                        task,
                        TaskStatus.FAILED,
                        task.stage,
                        issues=(Issue(code=code, message=message),),
                    )
                )
            task = self.steps.repository.get_task(state["task_id"])
            return PreparationState(task_id=task.task_id, data_version=task.data_version)

        return node

    def _confirm(self, state: PreparationState) -> PreparationState:
        """暂停等待补充，恢复时只提交经验证的确认结果。

        Args:
            state: 待确认任务引用。

        Returns:
            确认后增加的数据版本引用。
        """
        task = self.steps.repository.get_task(state["task_id"])
        response: object = interrupt(
            {
                "task_id": task.task_id,
                "issues": [issue.model_dump(mode="json") for issue in task.issues],
                "options": task.options.model_dump(mode="json"),
                "confirmation_schema": Confirmation.model_json_schema(),
            }
        )
        if not isinstance(response, str):
            raise PreparationError("INVALID_CONFIRMATION", "续跑数据必须是已验证的确认 JSON")
        confirmation = Confirmation.model_validate_json(response)
        self.steps.confirm(task.task_id, confirmation)
        updated = self.steps.repository.get_task(task.task_id)
        return PreparationState(task_id=updated.task_id, data_version=updated.data_version)

    def _after_parse(self, state: PreparationState) -> Literal["identify", "end"]:
        """根据解析结果选择识别或结束。

        Args:
            state: 当前任务引用。

        Returns:
            下一节点标识。
        """
        return (
            "end"
            if self.steps.repository.get_task(state["task_id"]).status == TaskStatus.FAILED
            else "identify"
        )

    def _after_identify(self, state: PreparationState) -> Literal["prepare", "confirm", "end"]:
        """根据字段识别结果选择准备、等待或结束。

        Args:
            state: 当前任务引用。

        Returns:
            下一节点标识。
        """
        status = self.steps.repository.get_task(state["task_id"]).status
        return (
            "end"
            if status == TaskStatus.FAILED
            else "confirm"
            if status == TaskStatus.NEEDS_INPUT
            else "prepare"
        )

    def _after_prepare(self, state: PreparationState) -> Literal["confirm", "end"]:
        """根据准备结果选择等待补充或结束。

        Args:
            state: 当前任务引用。

        Returns:
            下一节点标识。
        """
        return (
            "confirm"
            if self.steps.repository.get_task(state["task_id"]).status == TaskStatus.NEEDS_INPUT
            else "end"
        )
