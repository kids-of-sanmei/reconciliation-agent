"""Excel 文件上传接口。"""

from typing import Annotated, Final

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse

from app.api.dependencies import get_upload_service
from app.schemas.uploads import UploadErrorResponse, UploadResult
from app.services.errors import (
    EmptyUploadError,
    InvalidExcelError,
    InvalidHeaderRangeError,
    UnsupportedExcelTypeError,
    UploadSizeLimitError,
)
from app.services.uploads import ExcelUploadService

router: Final[APIRouter] = APIRouter()


@router.post(
    "/upload",
    status_code=status.HTTP_201_CREATED,
    response_model=UploadResult,
    summary="上传 Excel 并保存表头结构",
    responses={
        400: {"model": UploadErrorResponse, "description": "上传文件为空"},
        413: {"model": UploadErrorResponse, "description": "文件超过 20 MB"},
        415: {"model": UploadErrorResponse, "description": "文件扩展名不受支持"},
        422: {"description": "文件无法读取、表头范围无效或请求参数不合法"},
    },
)
def upload_excel(
    file: Annotated[UploadFile, File(description="Excel 文件（.xlsx 或 .xls）")],
    upload_service: Annotated[ExcelUploadService, Depends(get_upload_service)],
    header_start_row: Annotated[
        int | None, Form(gt=0, description="表头起始行，从 1 开始；留空使用首个非空行")
    ] = None,
    header_row_count: Annotated[
        int | None, Form(gt=0, description="表头行数；留空读取一行及跨行合并范围")
    ] = None,
) -> UploadResult:
    """保存 Excel 原文件及仅含表头的 JSON，不执行字段识别或对账。

    请求使用 multipart/form-data，file 字段仅支持 .xlsx、.xls，最大 20 MB。
    合并表头按左上角内容填充。多行表头可显式指定起始行和行数。
    同名文件使用不同编号独立保存；失败时清除本次文件。

    Args:
        file: 待保存的 Excel 文件，保留去除路径后的原始文件名。
        upload_service: 由依赖注入提供的文件校验及持久化服务。
        header_start_row: 所有工作表使用的表头起始行，默认首个非空行。
        header_row_count: 表头行数，默认一行并覆盖跨行合并。

    Returns:
        唯一编号、原始文件名、字节数及表头结构文件名。

    Raises:
        HTTPException: 空文件返回 400，超限返回 413，扩展名不支持返回 415。
            无法读取文件、表头范围无效或请求参数不合法时返回 422。
    """
    try:
        return upload_service.save(
            filename=file.filename, stream=file.file,
            header_start_row=header_start_row, header_row_count=header_row_count,
        )
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
    except (InvalidExcelError, InvalidHeaderRangeError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error


@router.get(
    "/uploads/{file_id}/headers.json",
    response_class=FileResponse,
    summary="下载已生成的表头 JSON",
    responses={
        200: {"content": {"application/json": {}}},
        404: {"model": UploadErrorResponse, "description": "表头 JSON 不存在"},
    },
)
def download_header_json(
    file_id: str,
    upload_service: Annotated[ExcelUploadService, Depends(get_upload_service)],
) -> FileResponse:
    """下载上传时保存的原始 JSON 文件，不重新解析工作簿。"""
    try:
        path = upload_service.get_structure_path(file_id=file_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    return FileResponse(path, media_type="application/json", filename=path.name)
