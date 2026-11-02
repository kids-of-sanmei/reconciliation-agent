"""第一块的 HTTP 接口：上传、实际进度、确认续跑和产物下载。"""

import io
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.preparation.application.service import PreparationService
from app.preparation.domain.errors import PreparationError
from app.preparation.domain.models import (
    PreparedData,
    TableProfiles,
    TaskOptions,
    TaskView,
)
from app.schema.preparation import ConfirmationBody

router = APIRouter(prefix="/api/preparation/tasks", tags=["数据准备"])


def get_preparation_service(request: Request) -> PreparationService:
    """获取应用工厂注入的服务。

    Args:
        request: 当前 HTTP 请求。

    Returns:
        初始化完成且类型明确的服务对象。
    """
    service: object = request.app.state.preparation_service
    if not isinstance(service, PreparationService):
        raise TypeError("数据准备服务未初始化")
    return service


ServiceDependency = Annotated[PreparationService, Depends(get_preparation_service)]


async def read_upload(upload: UploadFile, limit: int) -> bytes:
    """分块读取上传文件并限制实际大小。

    Args:
        upload: 框架提供的上传对象。
        limit: 允许的最大字节数。

    Returns:
        验证过长度的完整原始字节。
    """
    buffer = io.BytesIO()
    while chunk := await upload.read(min(1024 * 1024, limit + 1)):
        buffer.write(chunk)
        if buffer.tell() > limit:
            raise PreparationError("UPLOAD_TOO_LARGE", "上传文件超出大小限制", http_status=413)
    return buffer.getvalue()


@router.post("", status_code=202, response_model=TaskView)
async def create_task(
    background_tasks: BackgroundTasks,
    service: ServiceDependency,
    file_a: Annotated[UploadFile, File(description="A 业务底稿，当前支持 xlsx")],
    file_b: Annotated[UploadFile, File(description="B 系统出入库明细，当前支持 xlsx")],
    options: Annotated[str, Form(description="TaskOptions 的 JSON 字符串")] = "{}",
    reference_file: Annotated[
        UploadFile | None, File(description="可选的人工调整参考表，xlsx")
    ] = None,
) -> TaskView:
    """保存上传并在响应后启动准备流程。

    Args:
        background_tasks: 响应发送后的后台调度器。
        service: 数据准备应用服务。
        file_a: A 原始工作簿。
        file_b: B 原始工作簿。
        options: 包含 reconciliation_period 和可选映射的 JSON。
        reference_file: 可选人工调整表，保留来源供后续核查使用。

    Returns:
        received 状态及任务编号；用查询接口获取实际进度。
    """
    try:
        try:
            parsed_options = TaskOptions.model_validate_json(options)
        except ValidationError as exc:
            raise PreparationError(
                "INVALID_OPTIONS", "options 必须符合 TaskOptions JSON 契约"
            ) from exc
        a_content = await read_upload(file_a, service.settings.max_upload_bytes)
        b_content = await read_upload(file_b, service.settings.max_upload_bytes)
        reference_content = (
            await read_upload(reference_file, service.settings.max_upload_bytes)
            if reference_file is not None
            else None
        )
        task = await run_in_threadpool(
            service.create,
            file_a.filename or "",
            a_content,
            file_b.filename or "",
            b_content,
            parsed_options,
            reference_name=reference_file.filename if reference_file is not None else None,
            reference_content=reference_content,
        )
    finally:
        await file_a.close()
        await file_b.close()
        if reference_file is not None:
            await reference_file.close()
    background_tasks.add_task(service.run, task.task_id)
    return task


@router.get("/{task_id}", response_model=TaskView)
def get_task(task_id: str, service: ServiceDependency) -> TaskView:
    """查询任务状态。

    Args:
        task_id: 上传接口返回的编号。
        service: 应用服务。

    Returns:
        实际阶段、问题及已有产物。
    """
    return service.get(task_id)


@router.post("/{task_id}/run", response_model=TaskView)
def run_task(task_id: str, service: ServiceDependency) -> TaskView:
    """恢复进程中断或重试失败任务。

    Args:
        task_id: 已存在的任务编号。
        service: 应用服务。

    Returns:
        本轮执行结果；ready 任务不重复加工。
    """
    return service.run(task_id)


@router.post("/{task_id}/confirm", response_model=TaskView)
def confirm_task(
    task_id: str, confirmation: ConfirmationBody, service: ServiceDependency
) -> TaskView:
    """提交补充信息并从 LangGraph 中断续跑。

    Args:
        task_id: needs_input 状态的任务。
        confirmation: 人员、原因、完整替换配置和/或公式确认值。
        service: 应用服务。

    Returns:
        续跑后的状态和新增的数据版本。
    """
    return service.run(task_id, confirmation.root)


@router.get("/{task_id}/profiles", response_model=TableProfiles)
def get_profiles(task_id: str, service: ServiceDependency) -> TableProfiles:
    """提供人工映射所需结构摘要。

    Args:
        task_id: 已解析的任务。
        service: 应用服务。

    Returns:
        有界表头和样例摘要。
    """
    return service.profiles(task_id)


@router.get("/{task_id}/prepared", response_model=PreparedData)
def get_prepared(task_id: str, service: ServiceDependency) -> PreparedData:
    """提供下一块的正式输入契约。

    Args:
        task_id: 已就绪任务。
        service: 应用服务。

    Returns:
        PreparedData；未就绪时返回 409。
    """
    return service.prepared(task_id)


@router.get("/{task_id}/artifacts/{name}", response_class=FileResponse)
def download_artifact(task_id: str, name: str, service: ServiceDependency) -> FileResponse:
    """下载产物清单内的文件。

    Args:
        task_id: 已就绪任务。
        name: 文件名，例如 c_baseline.xlsx。
        service: 应用服务。

    Returns:
        对应文件内容和下载文件名。
    """
    path = service.artifact(task_id, name)
    media_type = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if path.suffix == ".xlsx"
        else "application/json"
    )
    return FileResponse(path, filename=path.name, media_type=media_type)
