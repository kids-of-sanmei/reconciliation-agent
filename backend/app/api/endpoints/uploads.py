"""Excel 文件上传接口。"""

from typing import Annotated, Final

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from app.api.dependencies import get_upload_service
from app.schemas.uploads import UploadErrorResponse, UploadResult
from app.services.errors import (
    EmptyUploadError,
    UnsupportedExcelTypeError,
    UploadSizeLimitError,
)
from app.services.uploads import ExcelUploadService

router: Final[APIRouter] = APIRouter()


@router.post(
    "/upload",
    status_code=status.HTTP_201_CREATED,
    response_model=UploadResult,
    summary="上传 Excel",
    responses={
        400: {"model": UploadErrorResponse, "description": "上传文件为空"},
        413: {"model": UploadErrorResponse, "description": "文件超过 20 MB"},
        415: {"model": UploadErrorResponse, "description": "文件扩展名不受支持"},
    },
)
def upload_excel(
    file: Annotated[UploadFile, File(description="Excel 文件（.xlsx 或 .xls）")],
    upload_service: Annotated[ExcelUploadService, Depends(get_upload_service)],
) -> UploadResult:
    """接收 Excel 原文件并持久化保存，不解析工作簿或触发对账。

    请求使用 multipart/form-data，file 字段仅支持 .xlsx、.xls，最大 20 MB。
    同名文件使用不同编号独立保存；上传失败时删除已写入的不完整文件。

    Args:
        file: 待保存的 Excel 文件，保留去除路径后的原始文件名。
        upload_service: 由依赖注入提供的文件校验及持久化服务。

    Returns:
        新建文件的唯一编号、原始文件名和实际保存的字节数。

    Raises:
        HTTPException: 空文件返回 400，超限返回 413，扩展名不支持返回 415。
            请求未提供 file 字段时，由请求校验返回 422。
    """
    try:
        return upload_service.save(filename=file.filename, stream=file.file)
    except EmptyUploadError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        ) from error
    except UploadSizeLimitError as error:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(error)
        ) from error
    except UnsupportedExcelTypeError as error:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(error)
        ) from error
