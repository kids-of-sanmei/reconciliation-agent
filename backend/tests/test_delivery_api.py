"""第三块真实 HTTP JSON、版本冲突及正式/诊断下载门槛回归。"""

from pathlib import Path

from tests.test_delivery import delivery_task
from tests.test_preparation_api import client_at


def test_delivery_http_complete_verify_and_diagnostic_gate(tmp_path: Path) -> None:
    """实际 HTTP 生成、复验、下载及能力不足门槛保持一致。

    Args:
        tmp_path: pytest 隔离存储目录。
    """
    task_id, _, match_version = delivery_task(tmp_path)
    base = f"/api/delivery/tasks/{task_id}"
    with client_at(tmp_path) as client:
        assert client.get(base).status_code == 404
        assert (
            client.post(
                f"{base}/run", json={"expected_match_plan_version": str(match_version)}
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"{base}/run", json={"expected_match_plan_version": match_version + 10}
            ).status_code
            == 409
        )
        response = client.post(f"{base}/run", json={"expected_match_plan_version": match_version})
        assert (
            response.status_code == 200 and response.json()["status"] == "completed_with_review"
        ), response.text
        state = client.get(base).json()
        assert (
            state["generation_version"] == 1
            and state["events"][-1]["stage"] == "completed_with_review"
        )
        result = client.get(f"{base}/result?expected_generation_version=1")
        assert result.status_code == 200 and len(result.json()["artifacts"]) == 6
        assert client.get(f"{base}/report?expected_generation_version=1").json()["passed"]
        for name in ("c_reconciled.xlsx", "b_annotated.xlsx"):
            download = client.get(f"{base}/artifacts/{name}?expected_generation_version=1")
            assert download.status_code == 200 and download.content.startswith(b"PK")
        assert (
            client.get(f"{base}/artifacts/unknown.json?expected_generation_version=1").status_code
            == 404
        )
        assert client.get(f"{base}/artifacts/c_reconciled.xlsx").status_code == 422
        assert (
            client.post(f"{base}/verify?expected_generation_version=1").json()["status"]
            == "completed_with_review"
        )
        blocked = client.post(
            f"{base}/run",
            json={
                "expected_match_plan_version": match_version,
                "options": {"display_engine": "unavailable"},
            },
        )
        assert blocked.status_code == 200 and blocked.json()["status"] == "needs_input"
        assert blocked.json()["generation_version"] == 2
        assert (
            client.get(
                f"{base}/artifacts/c_reconciled.xlsx?expected_generation_version=1"
            ).status_code
            == 409
        )
        assert (
            client.get(
                f"{base}/artifacts/c_reconciled.xlsx?expected_generation_version=2"
            ).status_code
            == 409
        )
        assert (
            client.get(
                f"{base}/diagnostics/verification_report.json?expected_generation_version=2"
            ).status_code
            == 200
        )
