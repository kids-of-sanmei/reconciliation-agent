"""Excel 原文件校验与本地持久化服务。"""

from pathlib import Path
from typing import BinaryIO, Final
from uuid import uuid4

from app.core.config import UploadSettings
from app.schemas.uploads import UploadResult
from app.services.errors import (
    EmptyUploadError,
    UnsupportedExcelTypeError,
    UploadError,
    UploadSizeLimitError,
)


class ExcelUploadService:
    """按独立编号保存原文件，统一执行类型、大小及非空校验。"""

    def __init__(self, *, settings: UploadSettings) -> None:
        self._settings: Final[UploadSettings] = settings

    def save(self, *, filename: str | None, stream: BinaryIO) -> UploadResult:
        """分块写入上传内容；校验或磁盘写入失败时清除本次残留文件。

        Args:
            filename: 客户端提供的文件名，用于确定扩展名及响应中的原文件名。
            stream: 已打开的二进制文件流，由调用方负责关闭。

        Returns:
            文件完成持久化后的编号、原文件名及实际字节数。

        Raises:
            UnsupportedExcelTypeError: 文件扩展名不受支持。
            EmptyUploadError: 文件内容为空。
            UploadSizeLimitError: 文件超过配置的大小上限。
            OSError: 创建目录、写入文件或清理残留文件失败。
        """
        original_filename: str = Path(filename or "").name
        extension: str = Path(original_filename).suffix.lower()
        if extension not in self._settings.allowed_extensions:
            raise UnsupportedExcelTypeError("仅支持 .xlsx 和 .xls 文件")

        file_id: str = uuid4().hex
        destination: Path = self._settings.directory / f"{file_id}{extension}"
        self._settings.directory.mkdir(parents=True, exist_ok=True)
        size: int = 0
        # 独占创建放在清理范围之外，避免编号冲突时误删已有文件。
        output: BinaryIO = destination.open("xb")
        try:
            with output:
                chunk: bytes
                while chunk := stream.read(self._settings.chunk_size_bytes):
                    size += len(chunk)
                    if size > self._settings.max_size_bytes:
                        raise UploadSizeLimitError("文件不能超过 20 MB")
                    output.write(chunk)
                if size == 0:
                    raise EmptyUploadError("不能上传空文件")
        except (UploadError, OSError):
            destination.unlink(missing_ok=True)
            raise

        return UploadResult(file_id=file_id, filename=original_filename, size=size)
