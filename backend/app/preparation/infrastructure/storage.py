"""任务级本地持久化，原子写入与跨进程锁支持中断恢复。"""

import hashlib
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID

from filelock import FileLock

from app.preparation.domain.errors import PreparationError
from app.preparation.domain.models import ArtifactRef, DomainModel, TaskView


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
        """原子保存任务状态。

        Args:
            task: 要替换的完整任务视图。
        """
        self.write_model(task.task_id, "task.json", task)

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
