"""任务应用入口：接收、并发控制及 LangGraph 检查点生命周期。"""

from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from uuid import uuid4

from filelock import Timeout
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from app.core.config import Settings
from app.preparation.application.steps import PreparationSteps
from app.preparation.domain.errors import PreparationError
from app.preparation.domain.models import (
    Confirmation,
    FileRole,
    PreparedData,
    SourceFile,
    TableProfiles,
    TaskOptions,
    TaskStatus,
    TaskView,
)
from app.preparation.infrastructure.llm import StructuredMappingAdvisor
from app.preparation.infrastructure.storage import LocalTaskRepository
from app.preparation.infrastructure.xlsx import XlsxGateway
from app.preparation.ports import MappingAdvisor, WorkbookGateway
from app.preparation.workflow.graph import PreparationGraph, PreparationState


class PreparationService:
    """接口层与第一块业务流程之间的稳定边界。"""

    def __init__(
        self,
        settings: Settings,
        *,
        workbook: WorkbookGateway | None = None,
        advisor: MappingAdvisor | None = None,
    ) -> None:
        """创建服务。

        Args:
            settings: 存储、资源限制和可选模型配置。
            workbook: 可替换工作簿适配器，默认精确处理 xlsx。
            advisor: 可注入的结构识别器，未提供时按环境配置决定。
        """
        self.settings = settings
        self.repository = LocalTaskRepository(settings.storage_root)
        gateway = workbook or XlsxGateway(
            max_archive_bytes=settings.max_archive_bytes, max_cells=settings.max_cells
        )
        if (
            advisor is None
            and settings.llm_model
            and settings.llm_api_key is not None
            and settings.llm_api_key.get_secret_value()
        ):
            advisor = StructuredMappingAdvisor(settings)
        self.steps = PreparationSteps(self.repository, gateway, advisor)

    def create(
        self,
        file_a_name: str,
        file_a: bytes,
        file_b_name: str,
        file_b: bytes,
        options: TaskOptions,
        *,
        reference_name: str | None = None,
        reference_content: bytes | None = None,
    ) -> TaskView:
        """保存原始输入并建任务。

        Args:
            file_a_name: A 原文件名，仅作展示及格式判断。
            file_a: A 原始文件字节。
            file_b_name: B 原文件名。
            file_b: B 原始文件字节。
            options: 已校验的对账年月和映射配置。
            reference_name: 可选人工调整表的原文件名。
            reference_content: 可选人工调整表原始字节，与文件名同时提供。

        Returns:
            received 状态的新任务，尚未解析工作簿。
        """
        inputs = [(FileRole.A, file_a_name, file_a), (FileRole.B, file_b_name, file_b)]
        if (reference_name is None) != (reference_content is None):
            raise PreparationError("INVALID_REFERENCE", "人工参考表需要同时提供文件名及内容")
        if reference_name is not None and reference_content is not None:
            inputs.append((FileRole.REFERENCE, reference_name, reference_content))
        for role, name, content in inputs:
            if Path(name).suffix.lower() != ".xlsx":
                raise PreparationError(
                    "UNSUPPORTED_FORMAT", "当前支持 .xlsx；.xls 和 CSV 尚未提供完整底稿保真适配"
                )
            if not content:
                raise PreparationError("EMPTY_FILE", "上传文件不能为空")
            if len(content) > self.settings.max_upload_bytes:
                raise PreparationError("UPLOAD_TOO_LARGE", "上传文件超出大小限制", http_status=413)
            if role == FileRole.A and PureWindowsPath(name).name.startswith("台州"):
                raise PreparationError(
                    "FILE_ROLE_CONFLICT", "台州前缀文件应作为 B 表，请核实上传角色"
                )
        task_id = str(uuid4())
        sources: list[SourceFile] = []
        for role, name, content in inputs:
            reference = self.repository.write_bytes(
                task_id, f"sources/input_{role.value}.xlsx", content
            )
            sources.append(
                SourceFile(
                    role=role,
                    file_id=f"file_{role.value}",
                    original_name=PureWindowsPath(name).name,
                    relative_path=reference.relative_path,
                    sha256=reference.sha256,
                    size_bytes=len(content),
                )
            )
        now = datetime.now(UTC)
        task = TaskView(
            task_id=task_id,
            status=TaskStatus.RECEIVED,
            stage="received",
            created_at=now,
            updated_at=now,
            sources=tuple(sources),
            options=options,
        )
        self.repository.save_task(task)
        return self.repository.get_task(task_id)

    def get(self, task_id: str) -> TaskView:
        """查询任务。

        Args:
            task_id: 已创建任务的编号。

        Returns:
            持久化的实际状态、配置和问题。
        """
        return self.repository.get_task(task_id)

    def run(self, task_id: str, confirmation: Confirmation | None = None) -> TaskView:
        """执行、恢复或重试持久化状态图。

        Args:
            task_id: 要执行的任务。
            confirmation: 对中断问题的可选补充，带人员及原因。

        Returns:
            执行到 ready、needs_input 或 failed 的任务视图。
        """
        task_id = self.repository.get_task(task_id).task_id
        try:
            with self.repository.lock(task_id):
                task = self.repository.get_task(task_id)
                if confirmation is not None:
                    if task.status != TaskStatus.NEEDS_INPUT:
                        raise PreparationError(
                            "TASK_NOT_WAITING", "任务当前未等待补充", http_status=409
                        )
                    if (
                        confirmation.expected_data_version is not None
                        and confirmation.expected_data_version != task.data_version
                    ):
                        raise PreparationError(
                            "STALE_CONFIRMATION",
                            "确认对应的数据版本已过期，请重新查询任务",
                            http_status=409,
                        )
                    self.steps.validate_confirmation(task_id, confirmation)
                elif task.status == TaskStatus.READY:
                    return task
                checkpoint_path = self.repository.directory(task_id) / "checkpoints.sqlite"
                with SqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
                    graph = PreparationGraph(self.steps).build(checkpointer)
                    config: RunnableConfig = {
                        "configurable": {"thread_id": task_id},
                        "recursion_limit": 100,
                    }
                    checkpoint = graph.get_state(config)
                    if confirmation is not None:
                        if not any(item.interrupts for item in checkpoint.tasks):
                            raise PreparationError(
                                "NO_PENDING_INTERRUPT",
                                "尚未形成可恢复中断，请先执行任务",
                                http_status=409,
                            )
                        self.repository.record_event(
                            task_id, "apply_confirmation", "started", "收到人工补充，开始恢复流程"
                        )
                        graph.invoke(Command(resume=confirmation.model_dump_json()), config)
                    elif checkpoint.next:
                        self.repository.record_event(
                            task_id, task.stage, "info", "从已保存的检查点恢复执行"
                        )
                        graph.invoke(None, config)
                    else:
                        self.repository.record_event(
                            task_id, "received", "started",
                            "重新执行数据准备" if task.events and task.status != TaskStatus.RECEIVED
                            else "开始执行数据准备",
                        )
                        graph.invoke(
                            PreparationState(task_id=task_id, data_version=task.data_version),
                            config,
                        )
                return self.repository.get_task(task_id)
        except Timeout as exc:
            raise PreparationError(
                "TASK_BUSY", "同一任务正在执行，请稍后查询进度", http_status=409
            ) from exc

    def profiles(self, task_id: str) -> TableProfiles:
        """提供人工映射所需的结构摘要。

        Args:
            task_id: 至少完成原始解析的任务。

        Returns:
            有界工作表表头和样例。
        """
        return self.repository.read_model(task_id, "sources/profiles.json", TableProfiles)

    def prepared(self, task_id: str) -> PreparedData:
        """读取供第二块使用的数据契约。

        Args:
            task_id: 已准备就绪的任务。

        Returns:
            包含版本和文件摘要的 PreparedData。
        """
        task = self.repository.get_task(task_id)
        if task.status != TaskStatus.READY or task.prepared_ref is None:
            raise PreparationError("NOT_PREPARED", "数据准备尚未完成", http_status=409)
        return self.repository.read_model(task_id, task.prepared_ref, PreparedData)

    def artifact(self, task_id: str, name: str) -> Path:
        """查找已发布产物，拒绝任意路径下载。

        Args:
            task_id: 已就绪的任务。
            name: 明确出现在产物清单中的文件名。

        Returns:
            当前版本的实际文件路径。
        """
        task = self.repository.get_task(task_id)
        if task.status != TaskStatus.READY:
            raise PreparationError("NOT_PREPARED", "数据准备尚未完成", http_status=409)
        if name not in task.artifact_names:
            raise PreparationError("ARTIFACT_NOT_FOUND", "产物不存在", http_status=404)
        return self.repository.path(task_id, f"versions/{task.data_version}/{name}")
