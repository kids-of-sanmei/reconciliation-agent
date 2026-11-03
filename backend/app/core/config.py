"""集中定义上传配置，路径始终相对于后端根目录。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Final

BACKEND_DIRECTORY: Final[Path] = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class UploadSettings:
    """不可变的上传配置，统一约束存储位置、文件类型及流式读取大小。"""

    directory: Path = BACKEND_DIRECTORY / ".data" / "uploads"
    max_size_bytes: int = 20 * 1024 * 1024
    chunk_size_bytes: int = 1024 * 1024
    allowed_extensions: frozenset[str] = frozenset({".xlsx", ".xls"})
