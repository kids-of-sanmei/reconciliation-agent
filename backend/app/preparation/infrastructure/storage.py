"""任务级本地持久化，原子写入与跨进程锁支持中断恢复。"""

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID

from filelock import FileLock

from app.preparation.domain.errors import PreparationError
from app.preparation.domain.models import (
    ArtifactRef,
    DomainModel,
    EventKind,
    TaskEvent,
    TaskStatus,
    TaskView,
)


class LocalTaskRepository:
    """按任务目录持久化文件；目录路径始终受 storage_root 约束。"""

    def __init__(self, storage_root: Path) -> None:
        """初始化仓库。

        Args:
            storage_root: 所有任务和 LangGraph 检查点的持久化根目录。
        """
        self.root = storage_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def directory(self, task_id: str) -> Path:
        """返回任务目录。

        Args:
            task_id: UUID 格式的任务标识。

        Returns:
            根目录下对应任务的绝对路径。
        """
        try:
            identifier = str(UUID(task_id))
        except ValueError as exc:
            raise PreparationError("INVALID_TASK_ID", "任务编号无效", http_status=404) from exc
        return self.root / "tasks" / identifier

    def path(self, task_id: str, relative_path: str) -> Path:
        """解析任务内部文件。

        Args:
            task_id: 所属任务。
            relative_path: 不含父级跳转的任务内相对路径。

        Returns:
            校验通过的绝对路径。
        """
        base = self.directory(task_id)
        relative = Path(relative_path)
        target = (base / relative).resolve()
        if relative.is_absolute() or ".." in relative.parts or not target.is_relative_to(base):
            raise PreparationError("INVALID_ARTIFACT_PATH", "文件路径超出任务范围")
        return target

    def lock(self, task_id: str) -> FileLock:
        """创建任务执行锁。

        Args:
            task_id: 要排他执行的任务。

        Returns:
            进程退出后可由操作系统释放的文件锁，冲突时立即失败。
        """
        directory = self.directory(task_id)
        directory.mkdir(parents=True, exist_ok=True)
        return FileLock(directory / "execution.lock", timeout=0)

    def write_bytes(self, task_id: str, relative_path: str, content: bytes) -> ArtifactRef:
        """原子写入文件并计算摘要。

        Args:
            task_id: 所属任务。
            relative_path: 输出相对路径。
            content: 完整文件字节。

        Returns:
            后续模块可验证内容的文件引用。
        """
        path = self.path(task_id, relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return ArtifactRef(
            name=path.name,
            relative_path=relative_path,
            sha256=hashlib.sha256(content).hexdigest(),
        )

    def write_model(self, task_id: str, relative_path: str, value: DomainModel) -> ArtifactRef:
        """序列化并保存领域对象。

        Args:
            task_id: 所属任务。
            relative_path: JSON 文件位置。
            value: 已通过类型和运行期校验的领域对象。

        Returns:
            JSON 文件引用。
        """
        return self.write_bytes(
            task_id, relative_path, value.model_dump_json(indent=2).encode("utf-8")
        )

    def read_model[T: DomainModel](
        self, task_id: str, relative_path: str, model_type: type[T]
    ) -> T:
        """读取并重新验证领域对象。

        Args:
            task_id: 所属任务。
            relative_path: JSON 文件位置。
            model_type: 期望的具体领域模型。

        Returns:
            完成严格 JSON 校验的对象。
        """
        path = self.path(task_id, relative_path)
        if not path.is_file():
            raise PreparationError("ARTIFACT_NOT_FOUND", "任务或文件不存在", http_status=404)
        return model_type.model_validate_json(path.read_bytes())

    def get_task(self, task_id: str) -> TaskView:
        """读取任务当前状态。

        Args:
            task_id: 要查询的任务。

        Returns:
            持久化的任务视图。
        """
        return self.read_model(task_id, "task.json", TaskView)

    def save_task(self, task: TaskView) -> None:
        """原子保存状态和阶段记录，保留执行期间新增的历史。

        Args:
            task: 要替换的完整任务视图。
        """
        previous = (
            self.get_task(task.task_id)
            if self.path(task.task_id, "task.json").is_file()
            else None
        )
        if previous is not None:
            task = task.model_copy(update={"events": previous.events})
        if previous is None or (previous.status, previous.stage) != (task.status, task.stage):
            titles = {
                "received": "原始文件已保存",
                "parse_workbooks": "开始解析工作簿",
                "identify_fields": "开始识别字段",
                "apply_confirmation": "人工补充已保存，继续执行",
                "normalize_and_build_baseline": "开始标准化明细",
                "normalize_records": "开始标准化明细",
                "build_baseline": "开始生成 C 初始底稿",
                "build_b_index": "开始建立 B 查询索引",
                "publish_prepared": "开始保存数据准备产物",
                "preparation_ready": "数据准备已完成",
            }
            title = titles.get(task.stage, "执行处理阶段")
            detail = ""
            kind: EventKind = "started"
            if task.status == TaskStatus.RECEIVED:
                kind = "completed"
                detail = "、".join(source.original_name for source in task.sources)
            elif task.status == TaskStatus.NEEDS_INPUT:
                kind, title = "waiting", "流程暂停，等待补充信息"
                detail = "；".join(issue.message for issue in task.issues)
            elif task.status == TaskStatus.FAILED:
                kind, title = "failed", "当前阶段执行失败"
                detail = "；".join(issue.message for issue in task.issues)
            elif task.status == TaskStatus.READY:
                kind = "completed"
                detail = f"已保存 {len(task.artifact_names)} 项产物，可下载 C 初始底稿和审计记录。"
            elif task.status == TaskStatus.PREPARING and task.stage == "identify_fields":
                kind, title = "completed", "字段及对账月份校验通过"
                detail = f"已确认 {len(task.options.mappings)} 个业务区域。"
            elif task.stage == "apply_confirmation":
                kind = "completed"
                confirmation = task.confirmations[-1].confirmation
                detail = f"{confirmation.actor}：{confirmation.reason}"
            task = self._with_event(task, task.stage, kind, title, detail)
        self.write_model(task.task_id, "task.json", task)

    def record_event(
        self, task_id: str, stage: str, kind: EventKind, title: str, detail: str = ""
    ) -> None:
        """在执行锁内追加真实动作；查询端始终读取完整的原子快照。

        Args:
            task_id: 当前执行任务。
            stage: 动作所属阶段。
            kind: 动作结果类型。
            title: 面向人员的动作名称。
            detail: 数量、工具结果或问题说明，不保存服务密钥。
        """
        task = self._with_event(self.get_task(task_id), stage, kind, title, detail)
        self.write_model(task_id, "task.json", task)

    def _with_event(
        self, task: TaskView, stage: str, kind: EventKind, title: str, detail: str
    ) -> TaskView:
        """构造带顺序、版本及真实时间的任务快照。

        Args:
            task: 最新持久化任务。
            stage: 动作所属阶段。
            kind: 动作结果类型。
            title: 动作名称。
            detail: 动作详情。

        Returns:
            保留已有记录并追加一个事件的不可变任务视图。
        """
        now = datetime.now(UTC)
        event = TaskEvent(
            sequence=len(task.events) + 1,
            occurred_at=now,
            data_version=task.data_version,
            stage=stage,
            kind=kind,
            title=title,
            detail=detail,
        )
        return task.model_copy(update={"events": (*task.events, event), "updated_at": now})

    def reference(self, task_id: str, relative_path: str) -> ArtifactRef:
        """读取已存在文件的可校验引用。

        Args:
            task_id: 所属任务。
            relative_path: 任务内文件位置。

        Returns:
            文件名称、位置和 SHA-256。
        """
        path = self.path(task_id, relative_path)
        return ArtifactRef(
            name=path.name,
            relative_path=relative_path,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
