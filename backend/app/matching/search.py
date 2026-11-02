"""规则驱动的候选计算，组合搜索预算、游标和差异均可追溯。"""

import hashlib
import itertools
from collections.abc import Iterator
from datetime import date
from fractions import Fraction

from app.matching.models import (
    AliasSuggestion,
    Allocation,
    Candidate,
    Check,
    MatchingRules,
    RelationType,
    RowProgress,
)
from app.matching.numbers import amount, number, numeric_issue, quantity, text
from app.preparation.domain.models import BusinessRecord, Direction


def canonical(value: str | None, rules: MatchingRules) -> str | None:
    """按任务确认的别名比较规格，检测循环映射。

    Args:
        value: 来源标准化规格；缺失时保留 None。
        rules: 当前版本的已确认别名规则。

    Returns:
        沿别名链归一后的规格；循环映射抛出 ValueError。
    """
    seen: set[str] = set()
    while value is not None and value in rules.specification_aliases:
        if value in seen:
            raise ValueError("规格别名存在循环")
        seen.add(value)
        target = rules.specification_aliases[value]
        if target == value:
            break
        value = target
    return value


def identity_compatible(a: BusinessRecord, b: BusinessRecord, rules: MatchingRules) -> bool:
    """缩小商品、单位、方向和日期范围，不以数量凑配候选。

    Args:
        a: 当前 A 明细或同侧组合基准行。
        b: 待比较的另一记录。
        rules: 已确认的别名及日期范围。

    Returns:
        存在商品或规格依据且无明确身份冲突时返回 True。
    """
    x, y = a.normalized_fields, b.normalized_fields
    if a.direction != b.direction or a.direction == Direction.UNKNOWN:
        if Direction.UNKNOWN not in {a.direction, b.direction}:
            return False
        try:
            if quantity(a) != 0 or quantity(b) != 0:
                return False
        except ValueError:
            return False
    for left, right in ((x.product_id, y.product_id), (x.unit, y.unit)):
        if left and right and left != right:
            return False
    if not x.date or not y.date:
        return False
    try:
        if (
            abs((date.fromisoformat(x.date) - date.fromisoformat(y.date)).days)
            > rules.date_tolerance_days
        ):
            return False
    except ValueError:
        return False
    same_spec = bool(x.spec and canonical(x.spec, rules) == canonical(y.spec, rules))
    same_product = bool(x.product_id and x.product_id == y.product_id)
    same_document = bool(x.document and x.document == y.document)
    same_name = bool(x.product_name and x.product_name == y.product_name)
    return same_spec or same_product or (same_document and same_name)


def homogeneous(records: tuple[BusinessRecord, ...], rules: MatchingRules) -> bool:
    """防止把不同商品、单位、方向或系统日期拼成组合。

    Args:
        records: 同一侧准备参与组合的来源记录。
        rules: 当前已确认规格别名。

    Returns:
        组合内每对记录均兼容且日期、单位、规格一致时返回 True。
    """
    for left, right in itertools.combinations(records, 2):
        x, y = left.normalized_fields, right.normalized_fields
        if (
            not identity_compatible(left, right, rules)
            or x.date != y.date
            or x.unit != y.unit
            or canonical(x.spec, rules) != canonical(y.spec, rules)
        ):
            return False
    return True


def compare(field: str, expected: Fraction, actual: Fraction, tolerance: str) -> Check:
    """按明确容差比较数值，保留真实差额。

    Args:
        field: 被比较的业务字段。
        expected: A 侧精确数值。
        actual: B 侧精确数值。
        tolerance: 当前规则明确设置的绝对容差。

    Returns:
        含通过标识及未经舍入差额的检查证据。
    """
    return Check(
        field=field,
        passed=abs(actual - expected) <= number(tolerance),
        expected=text(expected),
        actual=text(actual),
        delta=text(actual - expected),
    )


def make_candidate(
    a: tuple[BusinessRecord, ...],
    b: tuple[BusinessRecord, ...],
    rules: MatchingRules,
    search_alias: AliasSuggestion | None = None,
) -> Candidate:
    """计算单行、一对多、多对一或部分候选，绝不更改源值。

    Args:
        a: 候选对应的真实 A 记录。
        b: 候选对应的真实 B 记录，最多一侧包含多行。
        rules: 数值、日期、规格及预算的版本化口径。
        search_alias: 只对当前行生效的模型别名查询依据，不改变正式规则。

    Returns:
        保存原始标识、程序检查、差异和显式分配的候选。
    """
    aq = sum((quantity(r) for r in a), Fraction())
    bq = sum((quantity(r) for r in b), Fraction())
    am = sum((amount(r) for r in a), Fraction())
    bm = sum((amount(r) for r in b), Fraction())
    scoped = rules
    if search_alias is not None:
        scoped = rules.model_copy(
            update={
                "specification_aliases": {
                    **rules.specification_aliases,
                    search_alias.source: search_alias.target,
                }
            }
        )
    identity = all(identity_compatible(x, y, scoped) for x in a for y in b)
    coherent = homogeneous(a, rules) and homogeneous(b, rules)
    specs = {canonical(r.normalized_fields.spec, rules) for r in a + b}
    spec_equal = len(specs) == 1 and None not in specs
    dates = {r.normalized_fields.date for r in a + b}
    prices = [r.normalized_fields.unit_price for r in a + b]
    price_present = all(p is not None for p in prices)
    price_ok = price_present and all(
        abs(number(p) - number(prices[0])) <= number(rules.price_tolerance) for p in prices
    )
    partial = aq != bq
    relation: RelationType = (
        "partial"
        if partial
        else ("many_to_one" if len(a) > 1 else "one_to_many" if len(b) > 1 else "one_to_one")
    )
    checks: list[Check] = [
        Check(
            field="identity",
            passed=identity and coherent,
            expected="compatible",
            actual=str(identity and coherent),
        ),
        compare("quantity", aq, bq, rules.quantity_tolerance),
        compare("amount", am, bm, rules.amount_tolerance),
        Check(
            field="unit_price",
            passed=price_ok,
            expected=prices[0],
            actual=" | ".join(p or "missing" for p in prices),
            detail="单价缺失不视为零；每条组合记录分别校验",
        ),
        Check(
            field="date",
            passed=len(dates) == 1,
            expected=a[0].normalized_fields.date,
            actual=" | ".join(sorted(d or "missing" for d in dates)),
            detail="检索范围来自已确认日期容差",
        ),
        Check(
            field="spec",
            passed=spec_equal,
            expected=a[0].normalized_fields.spec,
            actual=" | ".join(sorted(r.normalized_fields.spec or "missing" for r in b)),
        ),
    ]
    reasons: list[str] = []
    if len(dates) != 1:
        reasons.append("日期不同，位于已确认日期范围内")
    if not spec_equal:
        reasons.append("规格关系需要语义或人工复核")
    if aq == 0 or am == 0 or bm == 0:
        reasons.append("零数量、零金额或补发件需复核")
    if a[0].direction == Direction.UNKNOWN:
        reasons.append("零数量业务方向需复核")
    for check in checks:
        if check.delta is not None and number(check.delta):
            reasons.append(f"{check.field} 差额 {check.delta}")
    allocations: list[Allocation] = []
    allocation_ok = True
    try:
        if partial:
            if not aq or not bq or not spec_equal or len(dates) != 1 or (len(a) > 1 and len(b) > 1):
                allocation_ok = False
            else:
                remaining = min(aq, bq)
                # 一侧为单行；同业务组合按来源顺序分配，任何未承接成员使该组合无效。
                for row in b if len(a) == 1 else a:
                    left = a[0] if len(a) == 1 else row
                    right = row if len(a) == 1 else b[0]
                    q = min(quantity(row), remaining)
                    if not q:
                        allocation_ok = False
                        break
                    allocated_a = amount(left) * q / quantity(left)
                    allocated_b = amount(right) * q / quantity(right)
                    allocations.append(
                        Allocation(
                            a_record_id=left.record_id,
                            b_record_id=right.record_id,
                            quantity=text(q),
                            a_amount=text(allocated_a),
                            b_amount=text(allocated_b),
                        )
                    )
                    checks.append(
                        compare("partial_amount", allocated_a, allocated_b, rules.amount_tolerance)
                    )
                    allocation_ok = allocation_ok and checks[-1].passed
                    remaining -= q
                reasons.append("部分数量对应，保留独立余额及组内分配顺序")
        elif len(a) == 1:
            for record in b:
                allocated_a = (
                    amount(record)
                    if am == bm
                    else (
                        am * quantity(record) / bq if bq else am * amount(record) / bm if bm else am
                    )
                )
                allocations.append(
                    Allocation(
                        a_record_id=a[0].record_id,
                        b_record_id=record.record_id,
                        quantity=text(quantity(record)),
                        a_amount=text(allocated_a),
                        b_amount=text(amount(record)),
                    )
                )
        else:
            for record in a:
                allocated_b = (
                    amount(record)
                    if am == bm
                    else (
                        bm * quantity(record) / aq if aq else bm * amount(record) / am if am else bm
                    )
                )
                allocations.append(
                    Allocation(
                        a_record_id=record.record_id,
                        b_record_id=b[0].record_id,
                        quantity=text(quantity(record)),
                        a_amount=text(amount(record)),
                        b_amount=text(allocated_b),
                    )
                )
    except ValueError as exc:
        allocation_ok = False
        reasons.append(str(exc))
        allocations.clear()
    eligible = identity and coherent and price_ok and allocation_ok
    if not partial:
        eligible = eligible and checks[1].passed and checks[2].passed
    checks.append(
        Check(field="allocation", passed=allocation_ok, expected="exact", actual=str(allocation_ok))
    )
    rule_id = f"{relation}.{'exact' if spec_equal and len(dates) == 1 else 'review'}"
    fingerprint = "|".join(
        [
            *(r.record_id for r in a),
            "/",
            *(r.record_id for r in b),
            rules.model_dump_json(),
            search_alias.model_dump_json() if search_alias else "",
        ]
    )
    return Candidate(
        candidate_id=hashlib.sha256(fingerprint.encode()).hexdigest()[:24],
        a_record_ids=tuple(r.record_id for r in a),
        b_record_ids=tuple(r.record_id for r in b),
        rule_id=rule_id,
        rule_version=rules.version,
        direction=a[0].direction,
        relation_type=relation,
        allocations=tuple(allocations),
        checks=tuple(checks),
        eligible=eligible,
        requires_advice=not spec_equal,
        review_reasons=tuple(reasons),
        search_alias=search_alias,
        evidence=tuple(
            dict.fromkeys(
                [*(r.record_id for r in a + b), *(search_alias.evidence if search_alias else ())]
            )
        ),
        tier="rejected"
        if not eligible
        else "partial"
        if partial
        else "review"
        if reasons
        else "strict",
    )


def search_space(
    current: BusinessRecord,
    a_records: tuple[BusinessRecord, ...],
    b_records: tuple[BusinessRecord, ...],
    rules: MatchingRules,
) -> Iterator[tuple[tuple[BusinessRecord, ...], tuple[BusinessRecord, ...]]]:
    """按固定顺序枚举单行和非连续组合，支持稳定游标重放。

    Args:
        current: 当前 A 主线记录。
        a_records: 全部 A 原始业务明细，用于多对一检索。
        b_records: 已按索引缩小的真实 B 记录。
        rules: 最大组合尺寸和比较规则。

    Yields:
        待程序验证的 A/B 组合，不代表已匹配。

    Returns:
        按稳定顺序生成待校验组合的迭代器。
    """
    pool = tuple(
        r
        for r in b_records
        if numeric_issue(r, is_a=False) is None and identity_compatible(current, r, rules)
    )
    for size in range(1, min(len(pool), rules.max_group_size) + 1):
        for combination in itertools.combinations(pool, size):
            yield (current,), combination
    # 同一多对一组合由原始顺序最前的 A 行负责，避免重复搜索和重复提交。
    position = next(i for i, r in enumerate(a_records) if r.record_id == current.record_id)
    peers = tuple(
        r
        for r in a_records[position + 1 :]
        if numeric_issue(r, is_a=True) is None and identity_compatible(current, r, rules)
    )
    for record in pool:
        for size in range(1, min(len(peers), rules.max_group_size - 1) + 1):
            for combination in itertools.combinations(peers, size):
                yield (current, *combination), (record,)


def search_row(
    current: BusinessRecord,
    a_records: tuple[BusinessRecord, ...],
    b_records: tuple[BusinessRecord, ...],
    rules: MatchingRules,
    progress: RowProgress,
    search_alias: AliasSuggestion | None = None,
) -> RowProgress:
    """在预算内推进一行检索，保留候选及未完成搜索范围。

    Args:
        current: 当前 A 原始业务记录。
        a_records: 用于多对一组合的原始 A 队列。
        b_records: 经 B 索引缩小的候选来源，不能删除重复内容行。
        rules: 固定版本的比较和资源规则。
        progress: 同一规则版本已保存的游标及候选。
        search_alias: 当前行经过来源验证的语义查询建议，默认不采用。

    Returns:
        更新的检查点；预算耗尽、组合尺寸或候选上限有独立未完成标记。
    """
    if numeric_issue(current, is_a=True) is not None:
        return progress.model_copy(update={"search_complete": True, "base_search_complete": True})
    candidates = {c.candidate_id: c for c in progress.candidates}
    scoped = (
        rules
        if search_alias is None
        else rules.model_copy(
            update={
                "specification_aliases": {
                    **rules.specification_aliases,
                    search_alias.source: search_alias.target,
                }
            }
        )
    )
    stream = itertools.islice(
        search_space(current, a_records, b_records, scoped), progress.cursor, None
    )
    cursor = progress.cursor
    exhausted = False
    capped = False
    singleton_count = sum(
        1
        for r in b_records
        if numeric_issue(r, is_a=False) is None and identity_compatible(current, r, scoped)
    )
    for _ in range(rules.search_budget):
        pair = next(stream, None)
        if pair is None:
            exhausted = True
            break
        candidate = make_candidate(*pair, rules, search_alias)
        if len(candidates) >= rules.max_candidates and candidate.candidate_id not in candidates:
            capped = True
            break
        candidates[candidate.candidate_id] = candidate
        cursor += 1
        if search_alias is None and cursor == singleton_count:
            strict = [c for c in candidates.values() if c.tier == "strict"]
            if len(strict) == 1:
                source = next(r for r in b_records if r.record_id == strict[0].b_record_ids[0])
                competing = any(
                    other.record_id != current.record_id
                    and numeric_issue(other, is_a=True) is None
                    and identity_compatible(other, source, rules)
                    and make_candidate((other,), (source,), rules).tier == "strict"
                    for other in a_records
                )
                if not competing:
                    return progress.model_copy(
                        update={
                            "cursor": cursor,
                            "search_scope": "strict_single",
                            "search_complete": True,
                            "base_search_complete": True,
                            "search_incomplete_reasons": (),
                            "candidates": tuple(candidates.values()),
                        }
                    )
    if not exhausted and not capped:
        exhausted = next(stream, None) is None
    pool_size = sum(
        1
        for r in b_records
        if identity_compatible(current, r, scoped) and numeric_issue(r, is_a=False) is None
    )
    peer_size = sum(
        1
        for r in a_records
        if identity_compatible(current, r, scoped) and numeric_issue(r, is_a=True) is None
    )
    reasons: list[str] = []
    if not exhausted:
        reasons.append("candidate_limit" if capped else "search_budget")
    if pool_size > rules.max_group_size or peer_size > rules.max_group_size:
        reasons.append("group_size_limit")
    return progress.model_copy(
        update={
            "cursor": cursor,
            "search_complete": not reasons,
            "base_search_complete": not reasons,
            "search_incomplete_reasons": tuple(reasons),
            "candidates": tuple(candidates.values()),
        }
    )
