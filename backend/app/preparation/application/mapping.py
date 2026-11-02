"""表头识别、结构摘要和映射校验，不依赖 HTTP 或具体模型。"""

import re
import unicodedata

from app.preparation.domain.models import (
    FieldName,
    FileRole,
    Issue,
    SheetMapping,
    SheetProfile,
    SourceSnapshots,
    TableProfiles,
    WorkbookSnapshot,
)
from app.preparation.domain.values import cell_text

A_ALIASES: dict[FieldName, tuple[str, ...]] = {
    FieldName.DATE: ("日期", "送货日期", "业务日期", "时间"),
    FieldName.SPEC: ("规格", "产品名称", "商品名称", "货品名称", "商品", "产品", "商品/规格名称"),
    FieldName.QUANTITY: ("数量", "送货数量", "业务数量"),
    FieldName.UNIT_PRICE: ("单价", "含税单价"),
    FieldName.AMOUNT: ("金额", "含税金额", "总金额"),
    FieldName.DOCUMENT: ("票据单号", "送货单号", "单据编号", "单号"),
    FieldName.PRODUCT_ID: ("货品编号", "货号", "产品编号"),
    FieldName.UNIT: ("单位",),
    FieldName.NOTE: ("备注", "摘要", "说明"),
}
B_ALIASES: dict[FieldName, tuple[str, ...]] = {
    FieldName.DATE: ("系统出入库时间", "出入库时间", "系统时间"),
    FieldName.WAREHOUSE: ("仓库", "仓库名称"),
    FieldName.DOCUMENT: ("单据编号", "系统单号"),
    FieldName.PRODUCT_ID: ("货品编号", "货号", "产品编号"),
    FieldName.PRODUCT_NAME: ("货品名称", "产品名称", "商品名称"),
    FieldName.SPEC: ("规格", "规格名称"),
    FieldName.IN_QUANTITY: ("入库数量",),
    FieldName.IN_PRICE: ("入库成本单价", "入库单价"),
    FieldName.IN_AMOUNT: ("入库成本金额", "入库金额"),
    FieldName.OUT_QUANTITY: ("出库数量",),
    FieldName.OUT_PRICE: ("出库成本单价", "出库单价"),
    FieldName.OUT_AMOUNT: ("出库成本金额", "出库金额"),
    FieldName.FIXED_COST: ("固定成本价",),
    FieldName.NOTE: ("备注", "摘要"),
}
A_REQUIRED = frozenset(
    {FieldName.DATE, FieldName.SPEC, FieldName.QUANTITY, FieldName.UNIT_PRICE, FieldName.AMOUNT}
)
B_REQUIRED = frozenset(set(B_ALIASES) - {FieldName.FIXED_COST, FieldName.NOTE})


def normalize_label(value: str) -> str:
    """标准化用于表头或规格检索的文字。

    Args:
        value: 原始文字，原数据不会被修改。

    Returns:
        统一全半角并移除空白的检索文本。
    """
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).strip()


def build_profiles(snapshots: SourceSnapshots) -> TableProfiles:
    """为模型构建有界的表结构摘要。

    Args:
        snapshots: 两个原始工作簿快照。

    Returns:
        每张表最多 20 行、每行 24 个非空样例的摘要。
    """
    profiles: list[SheetProfile] = []
    for workbook in snapshots.workbooks:
        for sheet in workbook.sheets:
            rows: dict[str, dict[str, str]] = {}
            for cell in sheet.cells.values():
                text = cell_text(cell)
                if cell.row > 30 or text is None:
                    continue
                key = str(cell.row)
                if key not in rows and len(rows) >= 20:
                    continue
                row = rows.setdefault(key, {})
                if len(row) < 24:
                    row[cell.column] = text[:120]
            profiles.append(
                SheetProfile(
                    role=workbook.source_file.role,
                    name=sheet.name,
                    max_row=sheet.max_row,
                    merged_ranges=sheet.merged_ranges,
                    sample_rows=rows,
                )
            )
    return TableProfiles(sheets=tuple(profiles))


def detect_mappings(workbook: WorkbookSnapshot) -> tuple[SheetMapping, ...]:
    """按已知表头识别确定性映射。

    Args:
        workbook: 原始工作簿。

    Returns:
        字段完整且没有重复字段歧义的候选映射，多个候选仍需范围确认。
    """
    aliases = A_ALIASES if workbook.source_file.role == FileRole.A else B_ALIASES
    required = A_REQUIRED if workbook.source_file.role == FileRole.A else B_REQUIRED
    result: list[SheetMapping] = []
    for sheet in workbook.sheets:
        for row_number in sorted({cell.row for cell in sheet.cells.values() if cell.row <= 50}):
            row_cells = [cell for cell in sheet.cells.values() if cell.row == row_number]
            fields: dict[FieldName, str] = {}
            ambiguous = False
            for field, labels in aliases.items():
                columns = [
                    cell.column
                    for cell in row_cells
                    if normalize_label(cell_text(cell) or "") in labels
                ]
                if len(columns) > 1:
                    ambiguous = True
                elif columns:
                    fields[field] = columns[0]
            if not ambiguous and required.issubset(fields) and row_number < sheet.max_row:
                result.append(
                    SheetMapping(
                        role=workbook.source_file.role,
                        sheet_name=sheet.name,
                        header_row=row_number,
                        data_start_row=row_number + 1,
                        data_end_row=sheet.max_row,
                        fields=fields,
                    )
                )
    return tuple(result)


def validate_mappings(
    snapshots: SourceSnapshots, mappings: tuple[SheetMapping, ...]
) -> tuple[Issue, ...]:
    """验证模型或人工提出的字段及范围。

    Args:
        snapshots: 不可变来源快照。
        mappings: 待采用的映射。

    Returns:
        缺少角色、重复工作表、越界或必要字段缺失等阻塞问题。
    """
    issues: list[Issue] = []
    for role in (FileRole.A, FileRole.B):
        if not any(mapping.role == role for mapping in mappings):
            issues.append(
                Issue(
                    code="MAPPING_REQUIRED",
                    message=f"请确认 {role.value.upper()} 表字段及工作表范围",
                )
            )
    seen: set[tuple[FileRole, str]] = set()
    for mapping in mappings:
        identity = (mapping.role, mapping.sheet_name)
        if identity in seen:
            issues.append(
                Issue(
                    code="DUPLICATE_MAPPING",
                    message="同一工作表只能配置一个业务区域；重复表头需明确分表或排除行",
                )
            )
            continue
        seen.add(identity)
        workbook = next(
            item for item in snapshots.workbooks if item.source_file.role == mapping.role
        )
        sheet = next((item for item in workbook.sheets if item.name == mapping.sheet_name), None)
        if sheet is None:
            issues.append(
                Issue(code="UNKNOWN_SHEET", message=f"工作表不存在：{mapping.sheet_name}")
            )
            continue
        required = A_REQUIRED if mapping.role == FileRole.A else B_REQUIRED
        missing = sorted(field.value for field in required - mapping.fields.keys())
        if missing:
            issues.append(
                Issue(code="MISSING_FIELDS", message=f"{sheet.name} 缺少字段：{', '.join(missing)}")
            )
        if mapping.data_end_row > sheet.max_row:
            issues.append(
                Issue(code="INVALID_RANGE", message=f"{sheet.name} 明细范围超出实际工作表")
            )
        for field, column in mapping.fields.items():
            if f"{column}{mapping.header_row}" not in sheet.cells:
                issues.append(
                    Issue(
                        code="INVALID_COLUMN",
                        message=f"{sheet.name} 的 {field.value} 列没有表头单元格",
                    )
                )
    return tuple(issues)
