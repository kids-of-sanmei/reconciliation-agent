"""本地模型协议、自动期间识别和实际汇总行结构的离线回归。"""

import io
import json
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
import pytest
from openpyxl import load_workbook

from app.core.config import Settings
from app.preparation.application.service import PreparationService
from app.preparation.domain.errors import PreparationError
from app.preparation.domain.models import MappingAdvice, RecordSet, TaskOptions, TaskStatus
from app.preparation.infrastructure.llm import OllamaMappingAdvisor
from tests.test_prompts import sample_profiles
from tests.workbooks import MAIN_NS, a_workbook, b_workbook


def test_ollama_uses_native_schema_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证原生端点、schema、无思考配置及无需密钥；monkeypatch 拦截网络。"""
    expected = MappingAdvice(mappings=(), explanation="无可确定的业务区域")

    def post(url: str, *, json: dict[str, object], timeout: float) -> httpx.Response:
        """校验请求并返回本地服务格式的响应。

        Args:
            url: 实际请求端点。
            json: 非流式结构化请求。
            timeout: 配置的推理超时。

        Returns:
            符合 Ollama 真实响应外壳的离线结果。
        """
        assert url == "http://127.0.0.1:11434/api/chat"
        assert timeout == 120
        schema = json["format"]
        assert isinstance(schema, dict)
        fields = schema["$defs"]["SheetMapping"]["properties"]["fields"]
        assert fields["additionalProperties"] is False
        assert fields["properties"]["date"] == {"type": "string", "pattern": "^[A-Z]{1,3}$"}
        assert json["stream"] is False and json["think"] is False
        assert json["options"] == {"temperature": 0, "num_ctx": 8192}
        assert "headers" not in json
        messages = json["messages"]
        assert isinstance(messages, list) and len(messages) == 2
        assert "sentinel_" in str(messages[1])
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"done": True, "message": {"content": expected.model_dump_json()}},
        )

    monkeypatch.setattr(httpx, "post", post)
    advisor = OllamaMappingAdvisor(
        Settings(
            llm_provider="ollama",
            llm_model="offline-local",
            llm_api_key=None,
            llm_base_url="http://127.0.0.1:11434/",
            llm_timeout_seconds=120,
            llm_context_tokens=8192,
        )
    )
    assert advisor.suggest(sample_profiles()) == expected


@pytest.mark.parametrize(
    "body",
    [
        {"error": "model unavailable"},
        {"done": False, "message": {"content": "{}"}},
        {"done": True, "done_reason": "length", "message": {"content": "{}"}},
        {"done": True, "message": {"content": "不是 JSON"}},
        {"done": True, "message": {"content": '{"mappings":[],"explanation":12}'}},
    ],
)
def test_ollama_rejects_incomplete_or_invalid_output(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, object]
) -> None:
    """验证错误外壳、截断及无效输出不会冒充有效映射。

    Args:
        monkeypatch: 拦截本地网络调用。
        body: 模拟的错误服务响应。
    """

    def post(url: str, **kwargs: object) -> httpx.Response:
        """返回 body 指定的响应；url 和 kwargs 为被拦截的请求参数。"""
        return httpx.Response(200, request=httpx.Request("POST", url), json=body)

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(PreparationError) as captured:
        OllamaMappingAdvisor(Settings(llm_model="offline-local")).suggest(sample_profiles())
    assert captured.value.code == "MODEL_MAPPING_UNAVAILABLE"


def test_ollama_network_failure_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证服务离线被转换为领域错误；monkeypatch 模拟连接失败。"""

    def post(url: str, **kwargs: object) -> httpx.Response:
        """模拟 url 对应的请求失败；kwargs 为调用参数。"""
        raise httpx.ConnectError("offline", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(PreparationError, match="Ollama"):
        OllamaMappingAdvisor(Settings(llm_model="offline-local")).suggest(sample_profiles())


def test_service_enables_ollama_without_secret(tmp_path: Path) -> None:
    """验证 provider 接线无需伪造密钥；tmp_path 为独立任务目录。"""
    service = PreparationService(
        Settings(storage_root=tmp_path, llm_provider="ollama", llm_model="local", llm_api_key=None)
    )
    assert isinstance(service.steps.advisor, OllamaMappingAdvisor)


def test_ollama_repairs_validation_error_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证错误的范围排除项交给模型修正；monkeypatch 模拟两次真实协议响应。"""
    attempts = 0

    def post(url: str, **kwargs: object) -> httpx.Response:
        """返回先错误后正确的模型结果；url、kwargs 为实际调用参数。"""
        nonlocal attempts
        attempts += 1
        advice = {
            "mappings": [
                {
                    "role": "a",
                    "sheet_name": "业务底稿",
                    "header_row": 2,
                    "data_start_row": 3,
                    "data_end_row": 5,
                    "fields": {
                        "date": "A",
                        "spec": "B",
                        "quantity": "C",
                        "unit_price": "D",
                        "amount": "E",
                    },
                    "excluded_rows": [6] if attempts == 1 else [],
                }
            ],
            "explanation": "合计行位于明细范围外，无需再次排除。",
        }
        if attempts == 2:
            payload = kwargs["json"]
            assert isinstance(payload, dict) and len(payload["messages"]) == 4
            assert "excluded_rows" in payload["messages"][-1]["content"]
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "done": True,
                "message": {"content": json.dumps(advice)},
            },
        )

    monkeypatch.setattr(httpx, "post", post)
    advice = OllamaMappingAdvisor(Settings(llm_model="offline-local")).suggest(sample_profiles())
    assert attempts == 2 and advice.mappings[0].excluded_rows == ()


def test_two_files_automatically_resolve_period_and_ignore_formula_footer(tmp_path: Path) -> None:
    """验证仅上传两表即可准备，且匿名 SUM 尾行不进入业务明细；tmp_path 隔离产物。"""
    workbook = load_workbook(io.BytesIO(b_workbook()))
    sheet = workbook.active
    assert sheet is not None
    sheet.append([None] * 10 + ["=SUM(K2:K7)", "=SUM(L2:L7)", "=SUM(M2:M7)"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    shared_buffer = io.BytesIO()
    with (
        ZipFile(io.BytesIO(buffer.getvalue())) as source,
        ZipFile(shared_buffer, "w", ZIP_DEFLATED) as target,
    ):
        for info in source.infolist():
            data = source.read(info)
            if info.filename == "xl/worksheets/sheet1.xml":
                root = ET.fromstring(data)
                for cell in root.iter(f"{{{MAIN_NS}}}c"):
                    if cell.get("r") in {"K8", "L8", "M8"}:
                        formula = cell.find(f"{{{MAIN_NS}}}f")
                        assert formula is not None
                        formula.set("t", "shared")
                        formula.set("si", "0")
                        if cell.get("r") == "K8":
                            formula.set("ref", "K8:M8")
                        else:
                            formula.text = None
                data = ET.tostring(root, encoding="utf-8")
            target.writestr(info, data)
    service = PreparationService(Settings(storage_root=tmp_path, llm_model=None))
    task = service.create("a.xlsx", a_workbook(), "b.xlsx", shared_buffer.getvalue(), TaskOptions())
    result = service.run(task.task_id)
    assert result.status == TaskStatus.READY, result.issues
    assert result.options.reconciliation_period == "2026-07"
    assert result.confirmations == ()
    assert any(event.title == "已自动识别对账月份" for event in result.events)
    profiles = service.profiles(task.task_id)
    assert profiles.sheets[-1].sample_formulas["K8"] == "SUM(K2:K7)"
    assert "8" in profiles.sheets[-1].tail_rows
    prepared = service.prepared(task.task_id)
    records = service.repository.read_model(
        task.task_id, prepared.b_records_ref.relative_path, RecordSet
    )
    assert len(records.records) == 7


def test_multiple_a_months_are_not_silently_reduced_to_one(tmp_path: Path) -> None:
    """验证跨月 A 数据不采用多数月份或当前年月；tmp_path 为隔离目录。"""
    workbook = load_workbook(io.BytesIO(a_workbook()))
    workbook["业务底稿"]["A5"] = "2026-08-01"
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    service = PreparationService(Settings(storage_root=tmp_path, llm_model=None))
    task = service.create("a.xlsx", buffer.getvalue(), "b.xlsx", b_workbook(), TaskOptions())
    result = service.run(task.task_id)
    assert result.status == TaskStatus.NEEDS_INPUT
    assert any(issue.code == "PERIOD_REQUIRED" for issue in result.issues)


def test_unknown_headers_go_through_ollama_and_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证陌生字段由本地协议识别后自动准备。

    Args:
        tmp_path: 隔离任务存储。
        monkeypatch: 只替换 Ollama HTTP 返回，保留实际识别链路。
    """

    def post(url: str, **kwargs: object) -> httpx.Response:
        """模拟 url 的结构化映射响应；kwargs 保留真实调用参数。"""
        advice = {
            "mappings": [
                {
                    "role": "a",
                    "sheet_name": "业务底稿",
                    "header_row": 2,
                    "data_start_row": 3,
                    "data_end_row": 5,
                    "fields": {
                        "date": "A",
                        "spec": "B",
                        "quantity": "C",
                        "unit_price": "D",
                        "amount": "E",
                    },
                }
            ],
            "explanation": "依据表头位置和业务样例识别，合计行不纳入明细。",
        }
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "done": True,
                "message": {"content": json.dumps(advice)},
            },
        )

    monkeypatch.setattr(httpx, "post", post)
    service = PreparationService(
        Settings(
            storage_root=tmp_path,
            llm_provider="ollama",
            llm_model="local",
            llm_api_key=None,
        )
    )
    task = service.create(
        "a.xlsx", a_workbook(unknown_headers=True), "b.xlsx", b_workbook(), TaskOptions()
    )
    result = service.run(task.task_id)
    assert result.status == TaskStatus.READY, result.issues
    assert result.options.reconciliation_period == "2026-07"
    assert result.confirmations == ()
    assert any(event.title == "调用模型辅助识别字段" for event in result.events)
