"""逐行匹配及整表仲裁的业务回归；全部使用真实十进制文本。"""

from pathlib import Path
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.matching.advisor import MatchingAdvisor
from app.matching.engine import reconcile
from app.matching.models import (
    AliasSuggestion,
    ManualDecision,
    MatchArtifacts,
    MatchConfirmation,
    MatchConfirmationRecord,
    MatchingRules,
    MatchingState,
    ModelProposal,
    RowContext,
    RowProgress,
)
from app.matching.numbers import number, text
from app.matching.search import make_candidate, search_row
from app.matching.service import MatchingService
from app.matching.validation import validate_candidate
from app.matching.workflow import RowState, RowWorkflow
from app.preparation.application.service import PreparationService
from app.preparation.domain.errors import PreparationError
from app.preparation.domain.models import (
    ArtifactRef,
    BusinessRecord,
    Direction,
    NormalizedFields,
    PreparedData,
    SourceLocation,
    TaskOptions,
)
from tests.workbooks import a_workbook, b_workbook, replace_cell_xml


def record(
    identifier: str,
    q: str,
    m: str,
    *,
    spec: str = "支架",
    price: str | None = "2",
    day: str | None = "2026-07-01",
    product: str | None = "SKU01",
    unit: str | None = "件",
    direction: Direction | None = None,
) -> BusinessRecord:
    """创建具有独立来源身份的精确业务记录。

    Args:
        identifier: 原始业务身份，不按内容去重。
        q: 精确数量文本。
        m: 精确金额文本。
        spec: 规格名称。
        price: 可缺失的单价。
        day: 可缺失的业务日期。
        product: 可缺失的货号。
        unit: 可缺失的单位。
        direction: 显式方向；未指定时按数量判断。

    Returns:
        不可变、带来源位置的业务记录。
    """
    return BusinessRecord(
        record_id=identifier,
        source=SourceLocation(
            file_id="file_a" if identifier.startswith("a") else "file_b",
            sheet_id="1",
            sheet_name="业务",
            row=2,
            cells={},
        ),
        raw_fields={},
        normalized_fields=NormalizedFields(
            date=day,
            spec=spec,
            quantity=q,
            unit_price=price,
            amount=m,
            product_id=product,
            unit=unit,
        ),
        direction=direction
        or (
            Direction.IN if number(q) > 0 else Direction.OUT if number(q) < 0 else Direction.UNKNOWN
        ),
        in_period=True,
    )


def state_for(
    a: tuple[BusinessRecord, ...],
    b: tuple[BusinessRecord, ...],
    rules: MatchingRules | None = None,
    advisor: MatchingAdvisor | None = None,
) -> MatchingState:
    """执行全部 A 行工作流并构造尚未仲裁的状态。

    Args:
        a: 原始顺序的 A 明细。
        b: 全部真实 B 记录。
        rules: 可选精确匹配口径。
        advisor: 可选语义模型替身。

    Returns:
        含每行搜索检查点的同版本状态。
    """
    selected = rules or MatchingRules()
    workflow = RowWorkflow(advisor).build()
    progress = tuple(
        workflow.invoke(
            RowState(
                current=r,
                a_records=a,
                b_records=b,
                rules=selected,
                progress=RowProgress(a_record_id=r.record_id),
            )
        )["progress"]
        for r in a
    )
    return MatchingState(
        task_id=str(uuid4()),
        prepared_data_ref=ArtifactRef(
            name="prepared.json", relative_path="versions/1/prepared.json", sha256="x"
        ),
        data_version=1,
        rules=selected,
        total_rows=len(a),
        completed_rows=len(a),
        progress=progress,
    )


def test_strict_single_and_returns_do_not_call_model() -> None:
    """正数入库、负数出库均按精确口径比较，无需语义调用。"""
    a = (record("a1", "3", "6"), record("a2", "-4", "-8", spec="退货"))
    b = (record("b1", "3", "6"), record("b2", "4", "8", spec="退货", direction=Direction.OUT))
    advisor = CountingAdvisor()
    result = reconcile(state_for(a, b, advisor=advisor), a, b)
    assert [r.status for r in result.rows] == ["matched", "matched"]
    assert advisor.calls == 0
    assert all(u.remaining_quantity == "0" for u in result.b_usage)
    assert a[1].normalized_fields.quantity == "-4"


def test_non_contiguous_one_to_many_and_many_to_one() -> None:
    """非连续来源组合和共享 B 的统一多对一组均显式分配。"""
    a = (record("a1", "462", "924"),)
    b = (
        record("b1", "449", "898"),
        record("b2", "9", "18", spec="别的商品", product="OTHER"),
        record("b3", "13", "26"),
    )
    result = reconcile(state_for(a, b), a, b)
    assert result.rows[0].status == "matched"
    assert result.groups[0].candidate.b_record_ids == ("b1", "b3")
    assert result.groups[0].candidate.relation_type == "one_to_many"
    a2 = (record("a1", "2", "4"), record("a2", "3", "6"))
    b2 = (record("b1", "5", "10"),)
    shared = reconcile(state_for(a2, b2), a2, b2)
    assert [r.status for r in shared.rows] == ["matched", "matched"]
    assert len(shared.groups) == 1
    assert shared.groups[0].candidate.relation_type == "many_to_one"
    assert [x.quantity for x in shared.groups[0].candidate.allocations] == ["2", "3"]


@pytest.mark.parametrize(
    ("aq", "bq", "a_remaining", "b_remaining"), [("5", "3", "2", "0"), ("3", "5", "0", "2")]
)
def test_partial_has_independent_status_and_exact_balances(
    aq: str, bq: str, a_remaining: str, b_remaining: str
) -> None:
    """部分承接保存两侧独立余额，不能标为普通已匹配。

    Args:
        aq: A 原始数量文本。
        bq: B 原始数量文本。
        a_remaining: 分配后预期的 A 剩余数量。
        b_remaining: 分配后预期的 B 剩余数量。
    """
    a = (record("a1", aq, text(number(aq) * 2)),)
    b = (record("b1", bq, text(number(bq) * 2)),)
    result = reconcile(state_for(a, b), a, b)
    assert result.rows[0].status == "partial"
    assert result.rows[0].remaining_quantity == a_remaining
    assert result.b_usage[0].remaining_quantity == b_remaining


def test_duplicate_sources_and_competition_are_not_greedily_consumed() -> None:
    """内容完全相同的 B 行仍有独立候选；A 竞争同一 B 时均待确认。"""
    a, b = (record("a1", "2", "4"),), (record("b1", "2", "4"), record("b2", "2", "4"))
    result = reconcile(state_for(a, b), a, b)
    assert result.rows[0].status == "unresolved"
    assert not result.groups and all(not u.group_ids for u in result.b_usage)
    assert len(result.rows[0].candidate_ids) >= 2
    a2 = (record("a1", "2", "4"), record("a2", "2", "4"))
    b2 = (record("b1", "2", "4"),)
    conflict = reconcile(state_for(a2, b2), a2, b2)
    assert [r.status for r in conflict.rows] == ["unresolved", "unresolved"]
    assert not conflict.groups


def test_zero_values_and_sign_conflict_remain_visible() -> None:
    """零值可形成带复核关系，符号冲突不强行判断方向。"""
    a = (record("a1", "0", "0", price="0"), record("a2", "2", "-4"))
    b = (record("b1", "0", "0", price="0"),)
    result = reconcile(state_for(a, b), a, b)
    assert [r.status for r in result.rows] == ["matched_review", "anomaly"]
    assert "符号冲突" in result.rows[1].reasons[0]


def test_numeric_differences_are_not_rounded_away() -> None:
    """来源超过 Decimal 默认精度仍保持精确；未配置容差不得通过。"""
    a = (record("a1", "1", "0.123456789012345678901234567890123456789"),)
    b = (record("b1", "1", "0.123456789012345678901234567890123456788"),)
    rejected = reconcile(state_for(a, b), a, b)
    assert rejected.rows[0].status == "unmatched"
    tolerant = reconcile(
        state_for(
            a, b, MatchingRules(amount_tolerance="0.000000000000000000000000000000000000001")
        ),
        a,
        b,
    )
    assert tolerant.rows[0].status == "matched_review"
    check = next(c for c in tolerant.groups[0].candidate.checks if c.field == "amount")
    assert check.delta == "-0.000000000000000000000000000000000000001"


def test_incompatible_product_unit_direction_and_price_cannot_be_combined() -> None:
    """不同商品、单位、方向、单价或金额无法仅靠数量相加通过。"""
    a = (record("a1", "5", "10"),)
    wrongs = (
        record("b1", "2", "4"),
        record("b2", "3", "6", product="OTHER"),
        record("b3", "3", "6", unit="箱"),
        record("b4", "3", "6", direction=Direction.OUT),
        record("b5", "3", "6", price="3"),
    )
    result = reconcile(state_for(a, wrongs), a, wrongs)
    assert all(g.status != "matched" for g in result.groups)
    assert all(c.relation_type != "one_to_many" for g in result.groups for c in (g.candidate,))


def test_budget_resume_and_combination_limit_are_explicit() -> None:
    """预算耗尽保存游标，续搜可找到非连续组合，组合上限仍标记未完成。"""
    a = (record("a1", "5", "10"),)
    b = (record("b1", "2", "4"), record("b2", "3", "6"))
    rules = MatchingRules(search_budget=1)
    row = RowProgress(a_record_id="a1")
    row = search_row(a[0], a, b, rules, row)
    assert not row.search_complete and row.cursor == 1
    row = search_row(a[0], a, b, rules, row)
    assert row.cursor == 2 and not row.search_complete
    row = search_row(a[0], a, b, rules, row)
    assert row.search_complete and row.cursor == 3
    assert any(c.relation_type == "one_to_many" and c.eligible for c in row.candidates)
    limited = state_for(a, b, MatchingRules(max_group_size=1))
    assert "group_size_limit" in limited.progress[0].search_incomplete_reasons
    assert reconcile(limited, a, b).rows[0].status == "unresolved"


class CountingAdvisor:
    """记录疑难模型调用并选择已验证候选。"""

    def __init__(self) -> None:
        """初始化实际调用计数。"""
        self.calls: int = 0

    def suggest(self, context: RowContext) -> ModelProposal:
        """给出有真实来源依据的规格语义建议。

        Args:
            context: 该轮真实输入。

        Returns:
            指向当前有效候选的建议，或保留未确定。
        """
        self.calls += 1
        eligible = [c for c in context.candidates if c.eligible and c.tier != "partial"]
        if eligible:
            return ModelProposal(
                candidate_id=eligible[0].candidate_id,
                specification_equivalent=True,
                reason="货号一致，站脚款为站脚简称，保留规格复核",
                evidence=eligible[0].evidence,
            )
        return ModelProposal(reason="证据不足")


def test_model_semantics_and_trace_are_program_validated() -> None:
    """模型只解释规格，程序检查数量、金额并保存真实输入输出。"""
    a = (record("a1", "462", "924", spec="站脚款"),)
    b = (record("b1", "449", "898", spec="站脚"), record("b2", "13", "26", spec="站脚"))
    advisor = CountingAdvisor()
    state = state_for(a, b, advisor=advisor)
    result = reconcile(state, a, b)
    assert result.rows[0].status == "matched_review" and advisor.calls == 1
    assert state.progress[0].proposals[0].context.a_record == a[0]
    assert state.progress[0].proposals[0].prompt_sha256


class HallucinatingAdvisor:
    """返回不存在的候选，验证建议不能直接写成正式关系。"""

    def suggest(self, context: RowContext) -> ModelProposal:
        """构造非法建议。

        Args:
            context: 当前行上下文。

        Returns:
            指向不存在候选和 B 记录的测试建议。
        """
        return ModelProposal(candidate_id="invented", reason="不可信", evidence=("invented_b",))


def test_hallucination_cannot_create_formal_usage() -> None:
    """模型不存在的候选和证据被拒绝，候选及问题仍可复核。"""
    a = (record("a1", "2", "4", spec="站脚款"),)
    b = (record("b1", "2", "4", spec="站脚"),)
    state = state_for(a, b, advisor=HallucinatingAdvisor())
    result = reconcile(state, a, b)
    assert result.rows[0].status == "unresolved" and not result.groups
    assert state.progress[0].proposals[0].error == "INVALID_MODEL_EVIDENCE"


class QueryAdvisor:
    """先提当前行别名查询，再选择程序新生成的候选。"""

    def suggest(self, context: RowContext) -> ModelProposal:
        """在限定轮次内请求真实规格别名查询。

        Args:
            context: 当前轮候选及真实来源。

        Returns:
            首轮查询建议或后续候选选择。
        """
        if context.candidates:
            c = context.candidates[0]
            return ModelProposal(
                candidate_id=c.candidate_id,
                specification_equivalent=True,
                reason="规格简称对应",
                evidence=c.evidence,
            )
        return ModelProposal(
            reason="查找规格简称",
            suggested_alias=AliasSuggestion(
                source="站脚款",
                target="站脚",
                evidence=(context.a_record.record_id, context.b_records[0].record_id),
            ),
        )


def test_new_alias_is_scoped_and_model_rounds_are_bounded() -> None:
    """建议别名只用于本行，经过程序重算后仍带规格复核。"""
    a = (record("a1", "2", "4", spec="站脚款", product=None),)
    b = (record("b1", "2", "4", spec="站脚", product=None),)
    state = state_for(a, b, advisor=QueryAdvisor())
    result = reconcile(state, a, b)
    assert result.rows[0].status == "matched_review"
    assert len(state.progress[0].proposals) == 2 and state.progress[0].tool_queries == 1
    assert not state.rules.specification_aliases
    limited = state_for(a, b, MatchingRules(max_model_rounds=1, max_tool_queries=0), QueryAdvisor())
    assert len(limited.progress[0].proposals) == 1 and limited.progress[0].tool_queries == 0


def test_forged_candidate_is_rejected_even_by_manual_confirmation() -> None:
    """直接篡改分配值无法绕过来源重算。"""
    a, b, rules = (record("a1", "2", "4"),), (record("b1", "2", "4"),), MatchingRules()
    c = make_candidate(a, b, rules)
    forged = c.model_copy(
        update={"allocations": (c.allocations[0].model_copy(update={"quantity": "99"}),)}
    )
    with pytest.raises(PreparationError, match="重新校验"):
        validate_candidate(forged, a, b, rules)


def prepared_service(path: Path, *, a_amount: str | None = None) -> tuple[str, MatchingService]:
    """以真实第一块产物建立可重启的第二块测试任务。

    Args:
        path: 隔离任务目录。
        a_amount: 可选的第一行金额覆盖，用于验证无法通过数值检查的真实候选。

    Returns:
        同一任务编号和禁用外部模型的匹配服务。
    """
    settings = Settings(storage_root=path, llm_model=None, llm_api_key=None)
    service = PreparationService(settings)
    content = a_workbook()
    if a_amount is not None:
        content = replace_cell_xml(content, "E3", value=a_amount)
    task = service.create(
        "a.xlsx", content, "b.xlsx", b_workbook(), TaskOptions(reconciliation_period="2026-07")
    )
    assert service.run(task.task_id).status.value == "ready"
    return task.task_id, MatchingService(settings)


def test_published_plan_restart_idempotency_and_manual_versions(tmp_path: Path) -> None:
    """真实交接、重启加载、人工接受、排除及旧版本拒绝形成闭环。

    Args:
        tmp_path: pytest 提供的隔离任务存储目录。
    """
    task_id, service = prepared_service(tmp_path)
    initial = service.run(
        task_id,
        MatchingRules(specification_aliases={"支架站脚款": "支架站脚", "支架滑轮款": "支架滑轮"}),
    )
    assert initial.status == "ready" and initial.completed_rows == 3
    plan = service.plan(task_id)
    assert plan.match_plan_version == 1
    restarted = MatchingService(Settings(storage_root=tmp_path, llm_model=None, llm_api_key=None))
    assert restarted.run(task_id) == initial
    assert restarted.artifacts(task_id) == service.artifacts(task_id)
    first = next(c for c in initial.progress[0].candidates if c.eligible)
    confirmed = service.confirm(
        task_id,
        MatchConfirmation(
            actor="会计",
            reason="已核实单据",
            expected_match_plan_version=1,
            decision=ManualDecision(action="accept", candidate_id=first.candidate_id),
        ),
    )
    assert confirmed.version == 2 and confirmed.status == "ready"
    assert any(g.confirmation_refs for g in service.artifacts(task_id).groups)
    with pytest.raises(PreparationError, match="版本已变化"):
        service.plan(task_id, expected_version=1)
    with pytest.raises(PreparationError, match="已过期"):
        service.confirm(
            task_id,
            MatchConfirmation(
                actor="会计",
                reason="重复操作",
                expected_match_plan_version=1,
                decision=ManualDecision(action="accept", candidate_id=first.candidate_id),
            ),
        )
    excluded = service.confirm(
        task_id,
        MatchConfirmation(
            actor="主管",
            reason="非本期业务，业务确认排除",
            expected_match_plan_version=2,
            decision=ManualDecision(action="exclude_b", b_record_ids=first.b_record_ids),
        ),
    )
    assert excluded.version == 3 and excluded.status == "ready"
    results = service.artifacts(task_id)
    assert all(not u.group_ids for u in results.b_usage if u.excluded)
    assert results.exclusions[0].reason == "非本期业务，业务确认排除"


def test_integrity_and_task_lock_are_enforced(tmp_path: Path) -> None:
    """来源被改写或同任务被占用时拒绝继续计算。

    Args:
        tmp_path: pytest 提供的隔离任务存储目录。
    """
    task_id, service = prepared_service(tmp_path)
    with service.repository.lock(task_id), pytest.raises(PreparationError, match="正在执行"):
        service.run(task_id)
    prepared = service.repository.get_task(task_id)
    path = service.repository.path(task_id, f"versions/{prepared.data_version}/b_records.json")
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(PreparationError, match="摘要不一致"):
        service.run(task_id)


def test_strict_single_does_not_expand_large_unneeded_combinations() -> None:
    """全表无竞争的严格单行结果不受后续组合尺寸限制影响。"""
    a = (record("a1", "100", "200"),)
    b = (record("b0", "100", "200"), *(record(f"b{i}", str(i), str(i * 2)) for i in range(1, 12)))
    state = state_for(a, b)
    assert state.progress[0].search_scope == "strict_single"
    assert state.progress[0].search_complete
    assert reconcile(state, a, b).rows[0].status == "matched"


def test_date_review_and_unknown_price_remain_distinct_from_matching() -> None:
    """日期差需要明确范围，缺单价不会按零通过检查。"""
    a = (record("a1", "2", "4"),)
    b = (record("b1", "2", "4", day="2026-07-02"),)
    assert reconcile(state_for(a, b), a, b).rows[0].status == "unmatched"
    reviewed = reconcile(state_for(a, b, MatchingRules(date_tolerance_days=1)), a, b)
    assert reviewed.rows[0].status == "matched_review"
    missing = (record("a1", "2", "4", price=None),)
    assert (
        reconcile(state_for(missing, b, MatchingRules(date_tolerance_days=1)), missing, b)
        .rows[0]
        .status
        == "unresolved"
    )


def test_many_to_one_partial_can_be_confirmed_as_a_unified_group() -> None:
    """多条 A 部分共享一条 B 时，确认统一组且 B 原值不被拆改。"""
    from datetime import UTC, datetime

    a = (record("a1", "2", "4"), record("a2", "3", "6"))
    b = (record("b1", "7", "14"),)
    state = state_for(a, b)
    candidate = next(
        c for p in state.progress for c in p.candidates if len(c.a_record_ids) == 2 and c.eligible
    )
    confirmation = MatchConfirmation(
        actor="复核员",
        reason="确认同一业务组分配",
        expected_match_plan_version=1,
        decision=ManualDecision(action="accept", candidate_id=candidate.candidate_id),
    )
    state = state.model_copy(
        update={
            "confirmations": (
                MatchConfirmationRecord(
                    confirmation_id="confirmation1",
                    confirmation=confirmation,
                    confirmed_at=datetime.now(UTC),
                    output_version=2,
                ),
            ),
            "version": 2,
        }
    )
    result = reconcile(state, a, b)
    assert [r.status for r in result.rows] == ["partial", "partial"]
    assert len(result.groups) == 1 and result.b_usage[0].remaining_quantity == "2"
    assert b[0].normalized_fields.quantity == "7"


def test_alias_query_cursor_and_tool_limit_are_retained() -> None:
    """语义组合查询未完成时不能伪装为搜完，并保留独立可续跑游标。"""
    a = (record("a1", "5", "10", spec="站脚款", product=None),)
    b = (
        record("b1", "2", "4", spec="站脚", product=None),
        record("b2", "3", "6", spec="站脚", product=None),
    )
    state = state_for(
        a, b, MatchingRules(search_budget=1, max_model_rounds=1, max_tool_queries=2), QueryAdvisor()
    )
    row = state.progress[0]
    assert not row.search_complete and row.alias_queries[0].cursor == 1
    assert reconcile(state, a, b).rows[0].status == "unresolved"
    workflow = RowWorkflow(None).build()
    resumed = workflow.invoke(
        RowState(current=a[0], a_records=a, b_records=b, rules=state.rules, progress=row)
    )["progress"]
    assert resumed.alias_queries[0].cursor == 2 and resumed.tool_queries == 2
    assert not resumed.search_complete and "tool_query_limit" in resumed.search_incomplete_reasons


class FailingPublishService(MatchingService):
    """模拟全部行执行完成后发布失败，检查重启不会重复模型调用。"""

    def _publish(
        self, state: MatchingState, prepared: PreparedData, result: MatchArtifacts
    ) -> MatchingState:
        """在原子任务头发布前模拟磁盘或下游工具失败。

        Args:
            state: 已保存全部行检查点的状态。
            prepared: 已验证的第一块输入。
            result: 已完成整表关系检查的结果。

        Returns:
            本替身总是抛出错误，不产生可下载的新方案。
        """
        raise OSError("模拟发布失败")


def test_failure_restart_and_search_revisions_preserve_progress(tmp_path: Path) -> None:
    """失败检查点可重启恢复，预算续搜产生新版本而不重复分配。

    Args:
        tmp_path: pytest 提供的隔离任务存储目录。
    """
    task_id, service = prepared_service(tmp_path)
    settings = Settings(storage_root=tmp_path, llm_model=None, llm_api_key=None)
    failing = FailingPublishService(settings)
    failed = failing.run(
        task_id, MatchingRules(search_budget=1, specification_aliases={"支架站脚款": "支架站脚"})
    )
    assert failed.status == "failed" and failed.completed_rows == 3 and failed.plan_ref is None
    recovered = service.run(task_id)
    assert recovered.status == "ready" and recovered.progress == failed.progress
    first_cursor = recovered.progress[0].cursor
    continued = service.continue_search(task_id, 1)
    assert continued.version == 2 and continued.progress[0].cursor > first_cursor
    assert service.plan(task_id).match_plan_version == 2
    assert all(len(u.group_ids) <= 1 for u in service.artifacts(task_id).b_usage)


def test_published_plan_revalidates_source_artifacts(tmp_path: Path) -> None:
    """发布后的来源文件被改写时，第三块入口同样拒绝旧摘要。

    Args:
        tmp_path: pytest 提供的隔离任务存储目录。
    """
    task_id, service = prepared_service(tmp_path)
    service.run(task_id)
    service.repository.path(task_id, "versions/1/b_records.json").write_text("{}", encoding="utf-8")
    with pytest.raises(PreparationError, match="摘要不一致"):
        service.plan(task_id)


def test_missing_numbers_are_not_zero_or_completed_no_match() -> None:
    """缺失必要数值独立阻塞，并保留不可计算余额，不能按零继续匹配。"""
    original = record("a1", "2", "4")
    missing_a = original.model_copy(
        update={"normalized_fields": original.normalized_fields.with_numeric_values(None, "2", "4")}
    )
    b = (record("b1", "2", "4"),)
    result = reconcile(state_for((missing_a,), b), (missing_a,), b)
    assert result.rows[0].status == "anomaly" and result.rows[0].remaining_quantity is None
    assert any(i.blocking and i.code == "A_NUMBER_UNAVAILABLE" for i in result.execution_issues)
    missing_b = b[0].model_copy(
        update={"normalized_fields": b[0].normalized_fields.with_numeric_values(None, "2", "4")}
    )
    result_b = reconcile(state_for((original,), (missing_b,)), (original,), (missing_b,))
    assert result_b.rows[0].status == "unresolved"
    assert any(i.blocking and i.code == "B_NUMBER_UNAVAILABLE" for i in result_b.execution_issues)


def test_recurring_partial_allocation_is_rejected_without_rounding() -> None:
    """循环小数分配不能按默认精度截断后登记部分或完整匹配。"""
    a = (record("a1", "3", "1"),)
    b = (record("b1", "1", "0.33"),)
    candidate = make_candidate(a, b, MatchingRules())
    assert not candidate.eligible and not candidate.allocations
    assert any("循环小数" in reason for reason in candidate.review_reasons)
