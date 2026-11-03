"""构造接口依赖，使接口无需管理服务的创建过程。"""

from app.core.config import UploadSettings
from app.services.uploads import ExcelUploadService


def get_upload_service() -> ExcelUploadService:
    """为上传接口提供使用统一存储配置的服务。"""
    return ExcelUploadService(settings=UploadSettings())
