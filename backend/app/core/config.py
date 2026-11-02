"""通过环境变量提供应用配置，密钥不写入业务数据。"""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """后端运行配置；路径默认定位 backend 目录，避免依赖启动目录。"""

    model_config = SettingsConfigDict(env_prefix="RECONCILIATION_", env_file=".env", extra="ignore")

    storage_root: Path = Path(__file__).resolve().parents[2] / ".data"
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    max_archive_bytes: int = Field(default=200 * 1024 * 1024, gt=0)
    max_cells: int = Field(default=500_000, gt=0)
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
