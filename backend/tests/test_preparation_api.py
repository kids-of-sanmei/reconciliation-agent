"""通过真实 HTTP 边界验证上传、查询、下载和人工续跑。"""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.factory import create_app
from tests.workbooks import a_workbook, b_workbook


def client_at(path: Path, *, upload_limit: int = 20 * 1024 * 1024) -> TestClient:
    """创建隔离客户端。

    Args:
        path: 任务存储目录。
        upload_limit: 用于测试大小校验的限制。

    Returns:
        不调用外部模型的真实 FastAPI 测试客户端。
    """
    return TestClient(
        create_app(
            Settings(
                storage_root=path, max_upload_bytes=upload_limit, llm_model=None, llm_api_key=None
            )
        )
    )


def test_upload_progress_prepared_and_download(tmp_path: Path) -> None:
    """验证完整 HTTP 准备和幂等重跑；tmp_path 为隔离目录。"""
    with client_at(tmp_path) as client:
        response = client.post(
            "/api/preparation/tasks",
            data={"options": '{"reconciliation_period":"2026-07"}'},
            files={"file_a": ("a.xlsx", a_workbook()), "file_b": ("b.xlsx", b_workbook())},
        )
        assert response.status_code == 202, response.text
        task_id = response.json()["task_id"]
        status = client.get(f"/api/preparation/tasks/{task_id}")
        assert status.status_code == 200 and status.json()["status"] == "ready"
        prepared = client.get(f"/api/preparation/tasks/{task_id}/prepared")
        assert prepared.status_code == 200
        assert prepared.json()["readiness"] == "ready"
        download = client.get(f"/api/preparation/tasks/{task_id}/artifacts/c_baseline.xlsx")
        assert download.status_code == 200 and download.content.startswith(b"PK")
        assert "c_baseline.xlsx" in download.headers["content-disposition"]
        assert (
            client.get(f"/api/preparation/tasks/{task_id}/artifacts/unknown.json").status_code
            == 404
        )
        assert client.post(f"/api/preparation/tasks/{task_id}/run").json()["data_version"] == 1
        assert client.get(f"/api/preparation/tasks/{task_id}/profiles").status_code == 200


def test_http_confirmation_accepts_json_mappings_and_recovers(tmp_path: Path) -> None:
    """验证 JSON 列表、枚举字段和人员确认能经过 HTTP 正确续跑；tmp_path 为隔离目录。"""
    with client_at(tmp_path) as client:
        response = client.post(
            "/api/preparation/tasks",
            files={
                "file_a": ("a.xlsx", a_workbook(unknown_headers=True)),
                "file_b": ("b.xlsx", b_workbook()),
            },
        )
        task_id = response.json()["task_id"]
        waiting = client.get(f"/api/preparation/tasks/{task_id}")
        assert waiting.json()["status"] == "needs_input"
        options = waiting.json()["options"]
        options["reconciliation_period"] = "2026-07"
        options["mappings"].append(
            {
                "role": "a",
                "sheet_name": "业务底稿",
                "header_row": 2,
                "data_start_row": 3,
                "data_end_row": 6,
                "fields": {
                    "date": "A",
                    "spec": "B",
                    "quantity": "C",
                    "unit_price": "D",
                    "amount": "E",
                },
            }
        )
        confirmed = client.post(
            f"/api/preparation/tasks/{task_id}/confirm",
            json={"actor": "财务", "reason": "核实字段和期间", "options": options},
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["status"] == "ready"
        assert confirmed.json()["data_version"] == 2


def test_http_rejects_invalid_parameters_and_unsupported_formats(tmp_path: Path) -> None:
    """验证参数、文件类型和任务标识边界；tmp_path 为隔离目录。"""
    with client_at(tmp_path) as client:
        files = {"file_a": ("a.xlsx", a_workbook()), "file_b": ("b.xlsx", b_workbook())}
        assert (
            client.post(
                "/api/preparation/tasks", data={"options": "bad json"}, files=files
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/preparation/tasks",
                data={"options": json.dumps({"reconciliation_period": "0000-07"})},
                files=files,
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/preparation/tasks",
                files={"file_a": ("a.csv", b"a,b\n1,2"), "file_b": files["file_b"]},
            ).status_code
            == 422
        )
        assert client.get("/api/preparation/tasks/not-a-uuid").status_code == 404
        assert (
            client.get("/api/preparation/tasks/00000000-0000-0000-0000-000000000000").status_code
            == 404
        )


def test_http_rejects_large_upload_without_creating_task(tmp_path: Path) -> None:
    """验证真实字节超限返回 413；tmp_path 为隔离目录。"""
    with client_at(tmp_path, upload_limit=10) as client:
        response = client.post(
            "/api/preparation/tasks",
            files={"file_a": ("a.xlsx", b"x" * 11), "file_b": ("b.xlsx", b"x")},
        )
        assert response.status_code == 413
        assert not (tmp_path / "tasks").exists()


def test_optional_reference_upload_and_strict_confirmation_boundary(tmp_path: Path) -> None:
    """验证参考表上传和强类型人员参数校验；tmp_path 为隔离目录。"""
    with client_at(tmp_path) as client:
        assert client.get("/").json() == {"message": "Hello World"}
        response = client.post(
            "/api/preparation/tasks",
            files={
                "file_a": ("a.xlsx", a_workbook()),
                "file_b": ("b.xlsx", b_workbook()),
                "reference_file": ("人工调整.xlsx", a_workbook()),
            },
        )
        task_id = response.json()["task_id"]
        assert len(client.get(f"/api/preparation/tasks/{task_id}").json()["sources"]) == 3
        invalid = client.post(
            f"/api/preparation/tasks/{task_id}/confirm", json={"actor": 123, "reason": "确认月份"}
        )
        assert invalid.status_code == 422
        blank = client.post(
            f"/api/preparation/tasks/{task_id}/confirm", json={"actor": "  ", "reason": "确认月份"}
        )
        assert blank.status_code == 422
        assert client.get(f"/api/preparation/tasks/{task_id}").json()["data_version"] == 1
