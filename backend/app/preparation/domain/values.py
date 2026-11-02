"""只依赖领域模型的来源值读取工具。"""

from app.preparation.domain.models import CellSnapshot


def cell_text(cell: CellSnapshot) -> str | None:
    """读取单元格可用值。

    Args:
        cell: 不可变的原始单元格快照。

    Returns:
        普通单元格原值或公式已有缓存，空值返回 None。
    """
    return cell.cached_value if cell.kind == "formula" else cell.value
