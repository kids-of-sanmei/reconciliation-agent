"""匹配启动、实际进度、复核、预算续搜和版本化产物接口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse

from app.matching.models import MatchArtifacts, MatchingState, MatchPlan
from app.matching.service import MatchingService
from app.schema.matching import MatchConfirmationBody, MatchingRulesBody

router = APIRouter(prefix="/api/matching/tasks", tags=["逐行匹配与复核"])


def get_matching_service(request: Request) -> MatchingService:
    """取得工厂注入的类型化服务。

    Args:
        request: 当前 HTTP 请求上下文。

    Returns:
        已初始化的匹配服务。
    """
    service: object = request.app.state.matching_service
    if not isinstance(service, MatchingService):
        raise TypeError("匹配服务未初始化")
    return service


ServiceDependency = Annotated[MatchingService, Depends(get_matching_service)]


@router.post("/{task_id}/run", response_model=MatchingState)
def run(
    task_id: str, service: ServiceDependency, rules: MatchingRulesBody | None = None
) -> MatchingState:
    """启动或恢复匹配，已完成版本幂等返回。

    Args:
        task_id: 第一块就绪任务编号。
        service: 注入的匹配服务。
        rules: 初次运行的可选完整规则；省略时继承第一块日期和别名口径。

    Returns:
        真实执行状态及最新版本。
    """
    return service.run(task_id, rules.root if rules is not None else None)


@router.get("/{task_id}", response_model=MatchingState)
def get(task_id: str, service: ServiceDependency) -> MatchingState:
    """查询实际行进度和候选、模型记录。

    Args:
        task_id: 匹配任务编号。
        service: 匹配服务。

    Returns:
        最近持久化的原子任务快照。
    """
    return service.get(task_id)


@router.get("/{task_id}/plan", response_model=MatchPlan)
def plan(
    task_id: str, service: ServiceDependency, expected_version: int | None = None
) -> MatchPlan:
    """读取供第三块消费的当前正式方案。

    Args:
        task_id: 匹配任务编号。
        service: 匹配服务。
        expected_version: 可选的消费者版本检查。

    Returns:
        已校验摘要和版本的 MatchPlan。
    """
    return service.plan(task_id, expected_version=expected_version)


@router.get("/{task_id}/results", response_model=MatchArtifacts)
def results(task_id: str, service: ServiceDependency) -> MatchArtifacts:
    """读取正式关系、行状态和待复核事项。

    Args:
        task_id: 匹配任务编号。
        service: 匹配服务。

    Returns:
        同一已发布版本的完整结果。
    """
    return service.artifacts(task_id)


@router.post("/{task_id}/confirm", response_model=MatchingState)
def confirm(
    task_id: str, confirmation: MatchConfirmationBody, service: ServiceDependency
) -> MatchingState:
    """人工确认后发布新方案，重新检查容量和关系。

    Args:
        task_id: 匹配任务编号。
        confirmation: 人员、原因、对象及当前预期版本。
        service: 匹配服务。

    Returns:
        重新仲裁后的最新版本状态。
    """
    return service.confirm(task_id, confirmation.root)


@router.post("/{task_id}/search", response_model=MatchingState)
def search(
    task_id: str, service: ServiceDependency, expected_version: Annotated[int, Query(ge=1)]
) -> MatchingState:
    """从保存的游标继续预算未完成搜索。

    Args:
        task_id: 匹配任务编号。
        service: 匹配服务。
        expected_version: 调用方持有的当前匹配版本。

    Returns:
        续搜后整表重算并发布的新版本。
    """
    return service.continue_search(task_id, expected_version)


@router.get("/{task_id}/artifacts/{name}", response_class=FileResponse)
def download(
    task_id: str,
    name: str,
    expected_version: Annotated[int, Query(ge=1)],
    service: ServiceDependency,
) -> FileResponse:
    """按版本下载白名单匹配文件。

    Args:
        task_id: 匹配任务编号。
        name: 正式匹配产物文件名。
        expected_version: 确保下载链接对应当前方案。
        service: 匹配服务。

    Returns:
        已通过摘要校验的 JSON 文件响应。
    """
    path = service.artifact(task_id, name, expected_version)
    return FileResponse(path, filename=name, media_type="application/json")
