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
        received = response.json()["events"]
        assert len(received) == 1 and received[0]["stage"] == "received"
        task_id = response.json()["task_id"]
        status = client.get(f"/api/preparation/tasks/{task_id}")
        assert status.status_code == 200 and status.json()["status"] == "ready"
        events = status.json()["events"]
        assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
        assert events[0] == received[0]
        assert events[-1]["stage"] == "preparation_ready"
        assert events[-1]["kind"] == "completed"
        assert {event["stage"] for event in events} >= {
            "received", "parse_workbooks", "identify_fields", "normalize_records",
            "build_baseline", "build_b_index", "publish_prepared", "preparation_ready",
        }
        assert any("A 表 3 条，B 表 7 条" in event["detail"] for event in events)
        assert not any(event["title"] == "调用模型辅助识别字段" for event in events)
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
        replay = client.post(f"/api/preparation/tasks/{task_id}/run").json()
        assert replay["data_version"] == 1 and replay["events"] == events
        assert client.get(f"/api/preparation/tasks/{task_id}/profiles").status_code == 200

    with client_at(tmp_path) as restarted:
        assert restarted.get(f"/api/preparation/tasks/{task_id}").json()["events"] == events


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
        waiting_events = waiting.json()["events"]
        assert waiting_events[-1]["kind"] == "waiting"
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
        events = confirmed.json()["events"]
        assert events[:len(waiting_events)] == waiting_events
        assert events[-1]["data_version"] == 2
        assert any(event["detail"] == "财务：核实字段和期间" for event in events)


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


def test_failed_execution_and_retry_preserve_real_history(tmp_path: Path) -> None:
    """失败记录保留实际阶段，重试追加历史且不会声称成功；tmp_path 为隔离目录。"""
    with client_at(tmp_path) as client:
        response = client.post(
            "/api/preparation/tasks",
            files={"file_a": ("a.xlsx", a_workbook()), "file_b": ("broken.xlsx", b"not an xlsx")},
        )
        url = f"/api/preparation/tasks/{response.json()['task_id']}"
        failed = client.get(url).json()
        assert failed["status"] == "failed"
        assert failed["events"][-1]["stage"] == "parse_workbooks"
        assert failed["events"][-1]["kind"] == "failed"
        assert any(event["title"] == "已解析 a.xlsx" for event in failed["events"])
        retried = client.post(f"{url}/run").json()
        assert retried["status"] == "failed"
        assert retried["events"][:len(failed["events"])] == failed["events"]
        assert len(retried["events"]) > len(failed["events"])
        assert not retried["artifact_names"]
        assert not any(event["stage"] == "preparation_ready" for event in retried["events"])


def test_legacy_task_read_does_not_invent_history(tmp_path: Path) -> None:
    """兼容旧任务且不补造过往动作；tmp_path 为隔离目录。"""
    with client_at(tmp_path) as client:
        response = client.post(
            "/api/preparation/tasks",
            files={"file_a": ("a.xlsx", a_workbook()), "file_b": ("b.xlsx", b_workbook())},
        )
        task_id = response.json()["task_id"]
        path = tmp_path / "tasks" / task_id / "task.json"
        stored = json.loads(path.read_text(encoding="utf-8"))
        del stored["events"]
        path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
        assert client.get(f"/api/preparation/tasks/{task_id}").json()["events"] == []
