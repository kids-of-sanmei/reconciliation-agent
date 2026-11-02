"""整表仲裁与余额计算；严格匹配优先，歧义不会因 A 顺序被静默消耗。"""

from fractions import Fraction

from app.matching.models import (
    ARowResult,
    BUsage,
    Candidate,
    MatchArtifacts,
    MatchGroup,
    MatchingState,
    ReviewItem,
    RowStatus,
)
from app.matching.numbers import amount, number, numeric_issue, quantity, text
from app.matching.search import identity_compatible
from app.matching.validation import advised_candidates, validate_candidate, validate_groups
from app.preparation.domain.errors import PreparationError
from app.preparation.domain.models import BusinessRecord, Issue


def manual_candidates(state: MatchingState, candidates: dict[str, Candidate]) -> dict[str, str]:
    """按审计顺序重放人工选择及释放，人工选择对受影响 A 行生效。

    Args:
        state: 当前规则版本及完整确认历史。
        candidates: 当前来源和规则下实际存在的候选。

    Returns:
        有效候选到最后确认记录标识的映射。
    """
    chosen: dict[str, str] = {}
    for record in state.confirmations:
        decision = record.confirmation.decision
        if decision.action == "rules":
            chosen.clear()
        elif decision.action == "release":
            chosen = {
                key: ref
                for key, ref in chosen.items()
                if not set(candidates[key].a_record_ids).intersection(decision.a_record_ids)
            }
        elif decision.action == "accept" and decision.candidate_id in candidates:
            candidate = candidates[decision.candidate_id]
            chosen = {
                key: ref
                for key, ref in chosen.items()
                if not set(candidates[key].a_record_ids).intersection(candidate.a_record_ids)
            }
            chosen[candidate.candidate_id] = record.confirmation_id
        elif decision.action == "exclude_b":
            chosen = {
                key: ref
                for key, ref in chosen.items()
                if not set(candidates[key].b_record_ids).intersection(decision.b_record_ids)
            }
    return chosen


def reconcile(
    state: MatchingState,
    a_records: tuple[BusinessRecord, ...],
    b_records: tuple[BusinessRecord, ...],
) -> MatchArtifacts:
    """将逐行候选统一为无重复使用的正式组、行结果和 B 余额。

    Args:
        state: 同一数据版本、规则版本的候选及人工审计。
        a_records: 保留来源顺序的全部 A 明细。
        b_records: 包括跨月、重复内容及异常记录的全部 B 明细。

    Returns:
        可独立发布的整表匹配结果和全部未确定事项。
    """
    candidates = {c.candidate_id: c for row in state.progress for c in row.candidates}
    progress = {r.a_record_id: r for r in state.progress}
    excluded = {r.b_record_id for r in state.exclusions}
    manual = manual_candidates(state, candidates)
    advice = advised_candidates(state.progress)
    groups: list[MatchGroup] = []
    occupied_a: set[str] = set()
    occupied_b: set[str] = set()
    for key, confirmation_ref in manual.items():
        c = candidates[key]
        validate_candidate(c, a_records, b_records, state.rules)
        groups.append(
            MatchGroup(
                group_id=c.candidate_id,
                candidate=c,
                status="partial"
                if c.relation_type == "partial"
                else "matched_review"
                if c.review_reasons
                else "matched",
                review_reasons=c.review_reasons,
                confirmation_refs=(confirmation_ref,),
            )
        )
        occupied_a.update(c.a_record_ids)
        occupied_b.update(c.b_record_ids)
    validate_groups(tuple(groups), a_records, b_records, state.rules, excluded)
    released: set[str] = set()
    for record in state.confirmations:
        decision = record.confirmation.decision
        if decision.action == "rules":
            released.clear()
        elif decision.action == "release":
            released.update(decision.a_record_ids)
        elif decision.action == "accept" and decision.candidate_id in candidates:
            released.difference_update(candidates[decision.candidate_id].a_record_ids)
    # 所有严格候选先统一处理，再处理复核关系和部分关系。
    for tier in ("strict", "review", "partial"):
        available = [
            c
            for c in candidates.values()
            if c.eligible
            and c.tier == tier
            and not excluded.intersection(c.b_record_ids)
            and not occupied_a.intersection(c.a_record_ids)
            and not occupied_b.intersection(c.b_record_ids)
            and not released.intersection(c.a_record_ids)
            and (not c.requires_advice or c.candidate_id in advice)
            and all(i in progress and progress[i].search_complete for i in c.a_record_ids)
        ]
        if tier != "strict":
            better = {
                i
                for c in candidates.values()
                if c.eligible
                and (c.tier == "strict" or tier == "partial" and c.tier == "review")
                and not excluded.intersection(c.b_record_ids)
                and not occupied_a.intersection(c.a_record_ids)
                and not occupied_b.intersection(c.b_record_ids)
                for i in c.a_record_ids
            }
            available = [c for c in available if not better.intersection(c.a_record_ids)]
        # 如果某 A 有模型选定的候选，其他同优先级候选仍保存但不自动承接。
        selected = {
            r.a_record_id: r.selected_candidate_id
            for r in state.progress
            if r.selected_candidate_id
        }
        available = [
            c
            for c in available
            if all(i not in selected or selected[i] == c.candidate_id for i in c.a_record_ids)
        ]
        a_competition: dict[str, set[str]] = {}
        b_competition: dict[str, set[str]] = {}
        for c in available:
            for i in c.a_record_ids:
                a_competition.setdefault(i, set()).add(c.candidate_id)
            for i in c.b_record_ids:
                b_competition.setdefault(i, set()).add(c.candidate_id)
        for c in available:
            if any(len(a_competition[i]) > 1 for i in c.a_record_ids) or any(
                len(b_competition[i]) > 1 for i in c.b_record_ids
            ):
                continue
            validate_candidate(c, a_records, b_records, state.rules)
            reasons = c.review_reasons + (
                (advice[c.candidate_id],) if c.candidate_id in advice else ()
            )
            groups.append(
                MatchGroup(
                    group_id=c.candidate_id,
                    candidate=c,
                    status="partial"
                    if tier == "partial"
                    else "matched_review"
                    if reasons
                    else "matched",
                    review_reasons=reasons,
                )
            )
            occupied_a.update(c.a_record_ids)
            occupied_b.update(c.b_record_ids)
    validate_groups(tuple(groups), a_records, b_records, state.rules, excluded)
    rows: list[ARowResult] = []
    reviews: list[ReviewItem] = []
    issues: list[Issue] = [issue for row in state.progress for issue in row.execution_issues]
    remaining_q: str | None
    remaining_m: str | None
    for a in a_records:
        related = tuple(c for c in candidates.values() if a.record_id in c.a_record_ids)
        adopted = tuple(g for g in groups if a.record_id in g.candidate.a_record_ids)
        row_progress = progress.get(a.record_id)
        anomaly = numeric_issue(a, is_a=True)
        reasons = ()
        status: RowStatus
        if adopted:
            status = adopted[0].status
            reasons = adopted[0].review_reasons
        elif anomaly:
            status, reasons = "anomaly", (anomaly,)
            if anomaly in {"数值缺失", "数值无法解析", "数值必须有限"}:
                issues.append(
                    Issue(code="A_NUMBER_UNAVAILABLE", message=anomaly, source_ref=a.record_id)
                )
        elif row_progress is None:
            status, reasons = "unresolved", ("行工作流尚未完成",)
            issues.append(
                Issue(code="ROW_PENDING", message="A 行缺少工作流结果", source_ref=a.record_id)
            )
        elif not row_progress.search_complete:
            status, reasons = (
                "unresolved",
                ("search_incomplete", *row_progress.search_incomplete_reasons),
            )
        elif related and any(c.eligible for c in related):
            status, reasons = "unresolved", ("候选歧义、竞争关系、人工释放或证据不足",)
        elif not a.normalized_fields.date or not a.normalized_fields.spec:
            status, reasons = "unresolved", ("日期或商品规格证据不足",)
        elif a.direction.value == "unknown" or quantity(a) == 0 or amount(a) == 0:
            status, reasons = "unresolved", ("零数量、零金额、补发件或方向不明需复核",)
        elif row_progress.execution_issues:
            status, reasons = "unresolved", tuple(i.message for i in row_progress.execution_issues)
        elif a.normalized_fields.unit_price is None:
            status, reasons = "unresolved", ("单价缺失，候选无法通过完整数值检查",)
        elif any(
            identity_compatible(a, b, state.rules)
            and (numeric_issue(b, is_a=False) is not None or b.normalized_fields.unit_price is None)
            for b in b_records
        ):
            status, reasons = "unresolved", ("相关 B 来源存在数值缺失或异常，不能判定无对应记录",)
        else:
            status, reasons = "unmatched", ("已完成规则范围内未发现可确认关系",)
        allocated_q = sum(
            (
                number(x.quantity)
                for g in adopted
                for x in g.candidate.allocations
                if x.a_record_id == a.record_id
            ),
            Fraction(),
        )
        allocated_m = sum(
            (
                number(x.a_amount)
                for g in adopted
                for x in g.candidate.allocations
                if x.a_record_id == a.record_id
            ),
            Fraction(),
        )
        try:
            remaining_q, remaining_m = (
                text(quantity(a) - allocated_q),
                text(amount(a) - allocated_m),
            )
        except ValueError:
            remaining_q = remaining_m = None
        rows.append(
            ARowResult(
                a_record_id=a.record_id,
                status=status,
                match_group_ids=tuple(g.group_id for g in adopted),
                candidate_ids=tuple(c.candidate_id for c in related),
                reasons=reasons,
                remaining_quantity=remaining_q,
                remaining_amount=remaining_m,
                progress_ref=f"matching/versions/{state.version}/progress.json#{a.record_id}",
            )
        )
        if status != "matched":
            reviews.append(
                ReviewItem(
                    code=status,
                    a_record_ids=(a.record_id,),
                    b_record_ids=tuple(dict.fromkeys(i for c in related for i in c.b_record_ids)),
                    candidate_ids=tuple(c.candidate_id for c in related),
                    reasons=reasons,
                )
            )
    usage: list[BUsage] = []
    remaining_q_b: str | None
    remaining_m_b: str | None
    for b in b_records:
        adopted = tuple(g for g in groups if b.record_id in g.candidate.b_record_ids)
        allocated_q = sum(
            (
                number(x.quantity)
                for g in adopted
                for x in g.candidate.allocations
                if x.b_record_id == b.record_id
            ),
            Fraction(),
        )
        allocated_m = sum(
            (
                number(x.b_amount)
                for g in adopted
                for x in g.candidate.allocations
                if x.b_record_id == b.record_id
            ),
            Fraction(),
        )
        try:
            remaining_q_b, remaining_m_b = (
                text(quantity(b) - allocated_q),
                text(amount(b) - allocated_m),
            )
        except ValueError:
            remaining_q_b = remaining_m_b = None
        usage.append(
            BUsage(
                b_record_id=b.record_id,
                group_ids=tuple(g.group_id for g in adopted),
                allocated_quantity=text(allocated_q),
                allocated_amount=text(allocated_m),
                remaining_quantity=remaining_q_b,
                remaining_amount=remaining_m_b,
                excluded=b.record_id in excluded,
            )
        )
        b_anomaly = numeric_issue(b, is_a=False)
        if b_anomaly:
            if (
                b_anomaly in {"数值缺失", "数值无法解析", "数值必须有限"}
                and b.record_id not in excluded
            ):
                issues.append(
                    Issue(code="B_NUMBER_UNAVAILABLE", message=b_anomaly, source_ref=b.record_id)
                )
            reviews.append(
                ReviewItem(code="b_anomaly", b_record_ids=(b.record_id,), reasons=(b_anomaly,))
            )
        elif b.issues:
            reviews.append(
                ReviewItem(
                    code="b_source_review",
                    b_record_ids=(b.record_id,),
                    reasons=tuple(issue.message for issue in b.issues),
                )
            )
    if len(rows) != len(a_records) or len({r.a_record_id for r in rows}) != len(rows):
        raise PreparationError("INVALID_A_COVERAGE", "A 行结果存在遗漏或重复")
    return MatchArtifacts(
        match_plan_version=state.version,
        rows=tuple(rows),
        groups=tuple(groups),
        b_usage=tuple(usage),
        reviews=tuple(reviews),
        exclusions=state.exclusions,
        execution_issues=tuple(issues),
    )
