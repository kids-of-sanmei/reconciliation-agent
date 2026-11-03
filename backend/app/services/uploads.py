"""Excel 上传校验、原文件保存及表头结构持久化。"""

from pathlib import Path
from re import fullmatch
from typing import BinaryIO, Final
from uuid import uuid4

from app.core.config import UploadSettings
from app.schemas.uploads import UploadResult
from app.services.errors import (
    EmptyUploadError,
    UnsupportedExcelTypeError,
    UploadSizeLimitError,
)
from app.services.headers import ExcelHeaderReader


class ExcelUploadService:
    """按独立编号保存原文件，统一执行类型、大小及非空校验。"""

    def __init__(self, *, settings: UploadSettings) -> None:
        self._settings: Final[UploadSettings] = settings

    def save(
        self,
        *,
        filename: str | None,
        stream: BinaryIO,
        header_start_row: int | None = None,
        header_row_count: int | None = None,
    ) -> UploadResult:
        """分块写入上传内容；校验或磁盘写入失败时清除本次残留文件。

        Args:
            filename: 客户端提供的文件名，用于确定扩展名及响应中的原文件名。
            stream: 已打开的二进制文件流，由调用方负责关闭。
            header_start_row: 表头首行，默认使用首个非空行。
            header_row_count: 表头行数，默认一行并完整展开跨行合并。

        Returns:
            文件编号、原文件名、实际字节数及表头结构文件名。

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
        structure_path = self._settings.directory / f"{file_id}.headers.json"
        self._settings.directory.mkdir(parents=True, exist_ok=True)
        size: int = 0
        # 独占创建放在清理范围之外，避免编号冲突时误删已有文件。
        output: BinaryIO = destination.open("xb")
        structure_created = False
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
            headers = ExcelHeaderReader().read(
                path=destination, file_id=file_id, filename=original_filename,
                header_start_row=header_start_row, header_row_count=header_row_count,
            )
            with structure_path.open("x", encoding="utf-8") as structure_output:
                structure_created = True
                structure_output.write(headers.model_dump_json(indent=2))
                structure_output.write("\n")
        except Exception:
            if structure_created:
                structure_path.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            raise

        return UploadResult(
            file_id=file_id, filename=original_filename, size=size,
            structure_filename=structure_path.name,
            structure_url=f"/api/uploads/{file_id}/headers.json",
        )

    def get_structure_path(self, *, file_id: str) -> Path:
        """只允许下载由上传编号定位的表头 JSON。"""
        if fullmatch(r"[0-9a-f]{32}", file_id) is None:
            raise FileNotFoundError("表头 JSON 文件不存在")
        path = self._settings.directory / f"{file_id}.headers.json"
        if not path.is_file():
            raise FileNotFoundError("表头 JSON 文件不存在")
        return path
