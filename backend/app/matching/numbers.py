"""用 Decimal 解析来源，以有理数执行无舍入的精确分配计算。"""

from decimal import Decimal, InvalidOperation
from fractions import Fraction

from app.preparation.domain.models import BusinessRecord


def number(value: str | None) -> Fraction:
    """解析有限十进制来源，缺失和错误不当作零。

    Args:
        value: 标准化数值文本；None 表示来源缺失。

    Returns:
        与来源十进制数完全相等的有理数。
    """
    if value is None:
        raise ValueError("数值缺失")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("数值无法解析") from exc
    if not parsed.is_finite():
        raise ValueError("数值必须有限")
    return Fraction(parsed)


def text(value: Fraction) -> str:
    """输出精确的有限十进制文本，禁止静默四舍五入。

    Args:
        value: 需要序列化的精确计算结果。

    Returns:
        无指数且不损失精度的十进制文本；循环小数抛出 ValueError。
    """
    denominator = value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise ValueError("分配金额为无限循环小数，需要人工提供可精确分配的关系")
    scale = max(twos, fives)
    integer = abs(value.numerator) * 2 ** (scale - twos) * 5 ** (scale - fives)
    digits = str(integer).zfill(scale + 1)
    result = digits if scale == 0 else digits[:-scale] + "." + digits[-scale:]
    return ("-" if value < 0 else "") + result


def quantity(record: BusinessRecord) -> Fraction:
    """取得不修改源值的数量比较口径。

    Args:
        record: A 或方向已拆分的 B 业务记录。

    Returns:
        来源数量的绝对值。
    """
    return abs(number(record.normalized_fields.quantity))


def amount(record: BusinessRecord) -> Fraction:
    """取得不修改源值的金额比较口径。

    Args:
        record: A 或方向已拆分的 B 业务记录。

    Returns:
        来源金额的绝对值。
    """
    return abs(number(record.normalized_fields.amount))


def numeric_issue(record: BusinessRecord, *, is_a: bool) -> str | None:
    """区分缺失、解析错误、方向和符号冲突，保留零值。

    Args:
        record: 待检查来源记录。
        is_a: 是否为通过数量符号确定方向的 A 行。

    Returns:
        异常原因；数值与方向均可解释时返回 None。
    """
    try:
        q = number(record.normalized_fields.quantity)
        m = number(record.normalized_fields.amount)
        if record.normalized_fields.unit_price is not None:
            price = number(record.normalized_fields.unit_price)
            if price < 0:
                return "单价为负，需核实业务口径"
        if is_a and q * m < 0:
            return "数量与金额符号冲突"
        if q and record.direction.value != ("in" if q > 0 else "out") and is_a:
            return "A 数量符号与标准化方向冲突"
        if not is_a and q * m < 0:
            return "B 数量与金额符号冲突"
        if record.direction.value == "unknown" and q:
            return "业务方向无法确定"
    except ValueError as exc:
        return str(exc)
    return None
