"""真实 HTTP 边界的匹配执行、确认、版本和下载回归。"""

from pathlib import Path

from tests.test_preparation_api import client_at


def test_matching_http_lifecycle_and_strict_json_boundary(tmp_path: Path) -> None:
    """上传后执行第二块，检查严格请求、版本更新及旧下载失效。

    Args:
        tmp_path: pytest 提供的隔离文件和任务存储目录。
    """
    from tests.workbooks import a_workbook, b_workbook

    with client_at(tmp_path) as client:
        created = client.post(
            "/api/preparation/tasks",
            data={"options": '{"reconciliation_period":"2026-07"}'},
            files={"file_a": ("a.xlsx", a_workbook()), "file_b": ("b.xlsx", b_workbook())},
        )
        assert created.status_code == 202
        task_id = created.json()["task_id"]
        base = f"/api/matching/tasks/{task_id}"
        assert client.get(base).status_code == 404
        assert client.post(f"{base}/run", json={"date_tolerance_days": "1"}).status_code == 422
        assert client.post(f"{base}/run", json={"amount_tolerance": 0.01}).status_code == 422
        started = client.post(
            f"{base}/run",
            json={"specification_aliases": {"支架站脚款": "支架站脚", "支架滑轮款": "支架滑轮"}},
        )
        assert started.status_code == 200, started.text
        assert started.json()["status"] == "ready"
        assert len(client.get(f"{base}/results").json()["rows"]) == 3
        assert client.post(f"{base}/run").json()["version"] == 1
        plan = client.get(f"{base}/plan").json()
        assert plan["prepared_data_version"] == 1
        download = client.get(f"{base}/artifacts/match_plan.json?expected_version=1")
        assert download.status_code == 200 and download.json() == plan
        assert client.get(f"{base}/artifacts/unknown.json?expected_version=1").status_code == 404
        assert client.get(f"{base}/artifacts/match_plan.json").status_code == 422
        progress = client.get(base).json()["progress"]
        candidate = next(c for row in progress for c in row["candidates"] if c["eligible"])
        body = {
            "actor": "复核员",
            "reason": "已核对原始业务单据",
            "expected_match_plan_version": 1,
            "decision": {"action": "accept", "candidate_id": candidate["candidate_id"]},
        }
        assert client.post(f"{base}/confirm", json={**body, "actor": " "}).status_code == 422
        accepted = client.post(f"{base}/confirm", json=body)
        assert accepted.status_code == 200 and accepted.json()["version"] == 2, accepted.text
        assert client.post(f"{base}/confirm", json=body).status_code == 409
        assert client.get(f"{base}/plan?expected_version=1").status_code == 409
        assert client.get(f"{base}/artifacts/match_plan.json?expected_version=1").status_code == 409
        assert (
            client.get(f"{base}/artifacts/confirmations.json?expected_version=2").json()["records"][
                0
            ]["confirmation"]["actor"]
            == "复核员"
        )


def test_manual_rules_require_new_version_and_failed_confirmation_is_atomic(tmp_path: Path) -> None:
    """非法候选或同名规则变更均不产生新版本，合法规则变化重新计算。

    Args:
        tmp_path: pytest 提供的隔离文件和任务存储目录。
    """
    import pytest

    from app.matching.models import ManualDecision, MatchConfirmation, MatchingRules
    from app.preparation.domain.errors import PreparationError
    from tests.test_matching import prepared_service

    task_id, service = prepared_service(tmp_path, a_amount="7285")
    first = service.run(task_id, MatchingRules(specification_aliases={"支架站脚款": "支架站脚"}))
    rejected = next(c for row in first.progress for c in row.candidates if not c.eligible)
    with pytest.raises(PreparationError):
        service.confirm(
            task_id,
            MatchConfirmation(
                actor="会计",
                reason="不能跳过数值检查",
                expected_match_plan_version=1,
                decision=ManualDecision(action="accept", candidate_id=rejected.candidate_id),
            ),
        )
    assert service.get(task_id) == first
    with pytest.raises(PreparationError, match="新版本"):
        service.confirm(
            task_id,
            MatchConfirmation(
                actor="会计",
                reason="改规则",
                expected_match_plan_version=1,
                decision=ManualDecision(action="rules", rules=MatchingRules(date_tolerance_days=1)),
            ),
        )
    assert service.get(task_id) == first
    updated = service.confirm(
        task_id,
        MatchConfirmation(
            actor="会计",
            reason="确认规格别名",
            expected_match_plan_version=1,
            decision=ManualDecision(
                action="rules",
                rules=MatchingRules(
                    version="matching-v2", specification_aliases={"支架滑轮款": "支架滑轮"}
                ),
            ),
        ),
    )
    assert updated.version == 2 and updated.status == "ready"
    assert service.plan(task_id).rule_version == "matching-v2"
