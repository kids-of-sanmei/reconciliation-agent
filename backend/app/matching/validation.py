"""正式关系的防线：从源值重新计算，不信任人工或模型提交的数值。"""

from fractions import Fraction

from app.matching.models import Candidate, MatchGroup, MatchingRules, RowProgress
from app.matching.numbers import amount, number, numeric_issue, quantity
from app.matching.search import make_candidate
from app.preparation.domain.errors import PreparationError
from app.preparation.domain.models import BusinessRecord


def validate_candidate(
    candidate: Candidate,
    a_records: tuple[BusinessRecord, ...],
    b_records: tuple[BusinessRecord, ...],
    rules: MatchingRules,
) -> None:
    """从真实记录重算候选，拒绝伪造记录、旧规则和数值差异。

    Args:
        candidate: 准备提交为正式关系的候选。
        a_records: 当前 PreparedData 的完整 A 集合。
        b_records: 当前 PreparedData 的完整 B 集合。
        rules: 当前方案绑定的比较规则。
    """
    a_by_id = {r.record_id: r for r in a_records}
    b_by_id = {r.record_id: r for r in b_records}
    try:
        a = tuple(a_by_id[i] for i in candidate.a_record_ids)
        b = tuple(b_by_id[i] for i in candidate.b_record_ids)
    except KeyError as exc:
        raise PreparationError("INVALID_MATCH_REFERENCE", "候选引用了不存在的来源记录") from exc
    if not a or not b or (len(a) > 1 and len(b) > 1):
        raise PreparationError("INVALID_MATCH_RELATION", "候选关系结构无效")
    if len(set(candidate.a_record_ids)) != len(a) or len(set(candidate.b_record_ids)) != len(b):
        raise PreparationError("DUPLICATE_MATCH_REFERENCE", "组内来源引用重复")
    if any(numeric_issue(r, is_a=True) for r in a) or any(numeric_issue(r, is_a=False) for r in b):
        raise PreparationError("INVALID_MATCH_NUMBERS", "来源存在数值或方向异常")
    alias = candidate.search_alias
    if alias is not None and (
        alias.source != a[0].normalized_fields.spec
        or alias.target not in {r.normalized_fields.spec for r in b}
        or not alias.evidence
        or not set(alias.evidence).issubset(set(a_by_id) | set(b_by_id))
        or not any(
            i in b_by_id and b_by_id[i].normalized_fields.spec == alias.target
            for i in alias.evidence
        )
    ):
        raise PreparationError("INVALID_ALIAS_EVIDENCE", "候选的行级别名不符合真实来源")
    recomputed = make_candidate(a, b, rules, alias)
    if recomputed != candidate or not recomputed.eligible:
        raise PreparationError("INVALID_MATCH_CANDIDATE", "候选未通过当前来源和规则的重新校验")


def validate_groups(
    groups: tuple[MatchGroup, ...],
    a_records: tuple[BusinessRecord, ...],
    b_records: tuple[BusinessRecord, ...],
    rules: MatchingRules,
    excluded: set[str],
) -> None:
    """检查整表容量和唯一关系；零值记录也不能重复独占。

    Args:
        groups: 本版本准备提交的全部正式匹配组。
        a_records: 原始 A 业务量。
        b_records: 原始 B 业务量。
        rules: 当前版本的数值规则。
        excluded: 有人工业务原因的 B 排除标识。
    """
    seen_a: set[str] = set()
    seen_b: set[str] = set()
    a_q: dict[str, Fraction] = {}
    a_m: dict[str, Fraction] = {}
    b_q: dict[str, Fraction] = {}
    b_m: dict[str, Fraction] = {}
    for group in groups:
        c = group.candidate
        validate_candidate(c, a_records, b_records, rules)
        if seen_a.intersection(c.a_record_ids) or seen_b.intersection(c.b_record_ids):
            raise PreparationError("DUPLICATE_MATCH_USAGE", "共享 B 的 A 行必须归入统一匹配组")
        if excluded.intersection(c.b_record_ids):
            raise PreparationError("EXCLUDED_MATCH_USAGE", "人工排除的 B 记录仍被正式使用")
        seen_a.update(c.a_record_ids)
        seen_b.update(c.b_record_ids)
        for allocation in c.allocations:
            if (
                allocation.a_record_id not in c.a_record_ids
                or allocation.b_record_id not in c.b_record_ids
            ):
                raise PreparationError("INVALID_ALLOCATION_REFERENCE", "分配引用超出正式匹配组")
            q, am, bm = (
                number(allocation.quantity),
                number(allocation.a_amount),
                number(allocation.b_amount),
            )
            if min(q, am, bm) < 0:
                raise PreparationError("NEGATIVE_ALLOCATION", "分配值必须使用非负比较口径")
            a_q[allocation.a_record_id] = a_q.get(allocation.a_record_id, Fraction()) + q
            a_m[allocation.a_record_id] = a_m.get(allocation.a_record_id, Fraction()) + am
            b_q[allocation.b_record_id] = b_q.get(allocation.b_record_id, Fraction()) + q
            b_m[allocation.b_record_id] = b_m.get(allocation.b_record_id, Fraction()) + bm
    for records, quantities, amounts in ((a_records, a_q, a_m), (b_records, b_q, b_m)):
        for record in records:
            if record.record_id in quantities and (
                quantities[record.record_id] > quantity(record)
                or amounts[record.record_id] > amount(record)
            ):
                raise PreparationError("ALLOCATION_EXCEEDS_SOURCE", "分配总量超过来源业务量")


def advised_candidates(progress: tuple[RowProgress, ...]) -> dict[str, str]:
    """提取已通过程序验证的模型选择及解释，不把未采用候选当作正式关系。

    Args:
        progress: 全部 A 行的工作流检查点。

    Returns:
        候选标识到已验证语义说明的映射。
    """
    result: dict[str, str] = {}
    for row in progress:
        for trace in row.proposals:
            if (
                trace.error is None
                and trace.proposal is not None
                and trace.proposal.candidate_id == row.selected_candidate_id
                and trace.proposal.candidate_id is not None
            ):
                result[trace.proposal.candidate_id] = trace.proposal.reason
    return result
