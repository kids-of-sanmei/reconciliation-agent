"""从 A 表业务日期推断明确的对账年月，不借用当前年份。"""

import re

from app.preparation.application.normalization import (
    is_structural_row,
    merged_date_cell,
    parse_business_date,
)
from app.preparation.domain.models import FieldName, FileRole, SheetMapping, SourceSnapshots
from app.preparation.domain.values import cell_text


def infer_reconciliation_period(
    snapshots: SourceSnapshots, mappings: tuple[SheetMapping, ...]
) -> str | None:
    """以 A 表明细的完整日期为依据推断唯一月份。

    Args:
        snapshots: 原始工作簿及其日期系统。
        mappings: 已经程序校验的字段及明细范围。

    Returns:
        有完整年份证据且所有明细属于同月时的 YYYY-MM，否则返回 None。
    """
    periods: set[str] = set()
    short_months: set[int] = set()
    for mapping in mappings:
        if mapping.role != FileRole.A:
            continue
        workbook = next(item for item in snapshots.workbooks if item.source_file.role == FileRole.A)
        sheet = next(item for item in workbook.sheets if item.name == mapping.sheet_name)
        for row in range(mapping.data_start_row, mapping.data_end_row + 1):
            if row in mapping.excluded_rows or is_structural_row(sheet, mapping, row):
                continue
            cell = merged_date_cell(sheet, mapping.fields[FieldName.DATE], row)
            value = cell_text(cell) if cell is not None else None
            if not value:
                return None
            short = re.fullmatch(r"\s*(\d{1,2})[月./-](\d{1,2})日?\s*", value)
            if short is not None:
                short_months.add(int(short.group(1)))
                continue
            try:
                # 此处只会接受完整日期或 Excel 日期序列，短日期已单独处理。
                parsed = parse_business_date(value, "0001-01", workbook.date_system)
                periods.add(f"{parsed.year:04d}-{parsed.month:02d}")
            except ValueError:
                return None
    if len(periods) != 1:
        return None
    period = next(iter(periods))
    return period if short_months <= {int(period[5:7])} else None
