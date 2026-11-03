"""上传接口的强类型响应模型。"""

from pydantic import BaseModel, ConfigDict, Field


class UploadResult(BaseModel):
    """成功保存的文件信息；严格校验类型，禁止修改已生成的响应数据。"""

    model_config = ConfigDict(strict=True, frozen=True)

    file_id: str = Field(description="本次上传生成的唯一文件编号")
    filename: str = Field(description="去除路径后的原始文件名")
    size: int = Field(gt=0, description="实际保存的文件大小，单位为字节")
    structure_filename: str = Field(description="仅包含表头结构的 JSON 文件名")
    structure_url: str = Field(description="表头 JSON 的下载地址，相对于 API 所在站点")


class UploadErrorResponse(BaseModel):
    """文件校验失败时返回的错误信息。"""

    model_config = ConfigDict(strict=True, frozen=True)

    detail: str = Field(description="上传失败的具体原因")
