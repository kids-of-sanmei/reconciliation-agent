"""制表、真实进度、独立复验及经过版本检查的正式/诊断下载。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse

from app.delivery.models import DeliveryResult, DeliveryState, VerificationReport
from app.delivery.service import DeliveryService
from app.schema.delivery import DeliveryRequestBody

router = APIRouter(prefix="/api/delivery/tasks", tags=["制表反向核查与交付"])


def get_service(request: Request) -> DeliveryService:
    """取得工厂注入的类型化交付服务。

    Args:
        request: 当前 HTTP 请求。

    Returns:
        已初始化的交付应用服务。
    """
    service: object = request.app.state.delivery_service
    if not isinstance(service, DeliveryService):
        raise TypeError("交付服务未初始化")
    return service


ServiceDependency = Annotated[DeliveryService, Depends(get_service)]
VersionQuery = Annotated[int, Query(ge=1)]


@router.post("/{task_id}/run", response_model=DeliveryState)
def run(task_id: str, request: DeliveryRequestBody, service: ServiceDependency) -> DeliveryState:
    """冻结输入，生成文件并独立验收。

    Args:
        task_id: 共用的准备/匹配任务编号。
        request: 预期匹配版本、能力和受限自动修复次数。
        service: 注入的交付服务。

    Returns:
        实际完成、复核、等待或失败状态。
    """
    return service.run(task_id, request.root)


@router.get("/{task_id}", response_model=DeliveryState)
def get(task_id: str, service: ServiceDependency) -> DeliveryState:
    """查询实际执行阶段和时间线。

    Args:
        task_id: 制表任务编号。
        service: 注入的应用服务。

    Returns:
        当前匹配方案下的真实交付状态。
    """
    return service.get(task_id)


@router.get("/{task_id}/result", response_model=DeliveryResult)
def result(
    task_id: str, expected_generation_version: VersionQuery, service: ServiceDependency
) -> DeliveryResult:
    """读取有版本、统计和实际验收结果的交付对象。

    Args:
        task_id: 制表任务编号。
        expected_generation_version: 调用方持有的生成版本。
        service: 注入的应用服务。

    Returns:
        内容摘要及版本一致的结果对象。
    """
    return service.result(task_id, expected_generation_version)


@router.post("/{task_id}/verify", response_model=DeliveryState)
def verify(
    task_id: str, expected_generation_version: VersionQuery, service: ServiceDependency
) -> DeliveryState:
    """重新读取已生成的实际文件独立复验。

    Args:
        task_id: 已产生文件的任务。
        expected_generation_version: 当前生成版本。
        service: 注入的交付服务。

    Returns:
        实际复验结果；不通过时撤销正式下载资格。
    """
    return service.recheck(task_id, expected_generation_version)


@router.get("/{task_id}/report", response_model=VerificationReport)
def report(
    task_id: str, expected_generation_version: VersionQuery, service: ServiceDependency
) -> VerificationReport:
    """查询真实文件的结构化验收结果。

    Args:
        task_id: 制表任务编号。
        expected_generation_version: 页面持有的生成版本。
        service: 注入的交付服务。

    Returns:
        pass、fail、not_run 分别列出的完整报告。
    """
    return service.report(task_id, expected_generation_version)


def file_response(path: str, name: str) -> FileResponse:
    """构造真实文件下载响应。

    Args:
        path: 已通过白名单、版本及摘要校验的绝对路径。
        name: 下载文件名。

    Returns:
        对应 xlsx、JSON 或 Markdown 的文件响应。
    """
    content_type = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if name.endswith(".xlsx")
        else "text/markdown; charset=utf-8"
        if name.endswith(".md")
        else "application/json"
    )
    return FileResponse(path, filename=name, media_type=content_type)


@router.get("/{task_id}/artifacts/{name}", response_class=FileResponse)
def artifact(
    task_id: str, name: str, expected_generation_version: VersionQuery, service: ServiceDependency
) -> FileResponse:
    """只下载已通过强制验收的正式文件。

    Args:
        task_id: 制表任务编号。
        name: 当前正式产物清单中的名称。
        expected_generation_version: 页面持有的生成版本。
        service: 注入的应用服务。

    Returns:
        当前正式文件，失败或旧版本返回冲突。
    """
    return file_response(str(service.artifact(task_id, name, expected_generation_version)), name)


@router.get("/{task_id}/diagnostics/{name}", response_class=FileResponse)
def diagnostic(
    task_id: str, name: str, expected_generation_version: VersionQuery, service: ServiceDependency
) -> FileResponse:
    """读取明确标记的诊断文件，不改变正式交付资格。

    Args:
        task_id: 制表任务编号。
        name: 当前诊断清单文件名。
        expected_generation_version: 当前生成版本。
        service: 注入的应用服务。

    Returns:
        诊断报告或待修复工作簿。
    """
    return file_response(
        str(service.artifact(task_id, name, expected_generation_version, diagnostic=True)), name
    )
