"""精确标准化 A/B 明细，保留原始身份、零值和方向，建立可重建索引。"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, Inexact, InvalidOperation, localcontext

from app.preparation.application.mapping import normalize_label
from app.preparation.domain.models import (
    BaselineTotals,
    BusinessRecord,
    CellSnapshot,
    Direction,
    FieldName,
    FileRole,
    Issue,
    LookupIndex,
    NormalizedFields,
    SheetMapping,
    SheetSnapshot,
    SourceLocation,
    SourceSnapshots,
    TaskOptions,
    WorkbookSnapshot,
)
from app.preparation.domain.values import cell_text


@dataclass(frozen=True)
class NormalizationResult:
    """供底稿生成和阶段验收使用的标准化结果。"""

    a_records: tuple[BusinessRecord, ...]
    b_records: tuple[BusinessRecord, ...]
    totals: tuple[BaselineTotals, ...]
    issues: tuple[Issue, ...]


def parse_decimal(value: str) -> Decimal:
    """解析业务数字，不做浮点运算或小数位截断。

    Args:
        value: 来源数字或明确的千分位、括号负数形式。

    Returns:
        有限的 Decimal。

    Raises:
        ValueError: 来源为空、不是数字或为 NaN/Infinity。
    """
    text = unicodedata.normalize("NFKC", value).strip().replace("−", "-")
    if re.fullmatch(r"\(\s*[\d,.]+\s*\)", text):
        text = "-" + text[1:-1].strip()
    if "," in text:
        if not re.fullmatch(r"[+-]?\d{1,3}(,\d{3})+(\.\d+)?", text):
            raise ValueError("千分位格式无效")
        text = text.replace(",", "")
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("无法解析数值") from exc
    if not result.is_finite():
        raise ValueError("数值必须有限")
    return result


def parse_business_date(value: str, period: str, epoch: str) -> date:
    """解析完整日期、只有月日的日期或 Excel 序列日期。

    Args:
        value: 来源日期文本或数字序列。
        period: 已确认年月，用于补足缺失年份。
        epoch: 工作簿使用的 1900 或 1904 日期系统。

    Returns:
        可用于月份判断的完整业务日期。

    Raises:
        ValueError: 日期不合法或无法解析。
    """
    text = unicodedata.normalize("NFKC", value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        serial = parse_decimal(text)
        days = int(serial)
        if epoch == "1900":
            if days == 60:
                raise ValueError("Excel 序列 60 表示不存在的 1900-02-29")
            base = date(1899, 12, 31) if days < 60 else date(1899, 12, 30)
        else:
            base = date(1904, 1, 1)
        try:
            return base + timedelta(days=days)
        except OverflowError as exc:
            raise ValueError("Excel 日期超出范围") from exc
    full = re.match(r"^(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})(?:日)?(?:$|[ T])", text)
    if full is not None:
        return date(int(full.group(1)), int(full.group(2)), int(full.group(3)))
    short = re.fullmatch(r"(\d{1,2})[月./-](\d{1,2})日?", text)
    if short is not None:
        return date(int(period[:4]), int(short.group(1)), int(short.group(2)))
    try:
        return datetime.fromisoformat(text).date()
    except ValueError as exc:
        raise ValueError("日期格式无法识别") from exc


def merged_date_cell(sheet: SheetSnapshot, column: str, row: int) -> CellSnapshot | None:
    """仅在原日期合并范围内读取左上角值。

    Args:
        sheet: 原始工作表。
        column: 日期列。
        row: 当前原始明细行。

    Returns:
        当前日期单元格或同列原合并区域的锚点，不跨区域向下填充。
    """
    current = sheet.cells.get(f"{column}{row}")
    if current is not None and cell_text(current) not in {None, ""}:
        return current
    for merged in sheet.merged_ranges:
        match = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", merged)
        if (
            match is not None
            and match.group(1) == column == match.group(3)
            and int(match.group(2)) <= row <= int(match.group(4))
        ):
            return sheet.cells.get(f"{column}{match.group(2)}")
    return current


def is_structural_row(sheet: SheetSnapshot, mapping: SheetMapping, row: int) -> bool:
    """判断空行、合计及重复表头。

    Args:
        sheet: 原始表。
        mapping: 已验证字段映射。
        row: 待判断原始行号。

    Returns:
        是否为无需进入业务匹配的结构行。
    """
    values = [
        cell_text(sheet.cells[f"{column}{row}"]) or ""
        for column in mapping.fields.values()
        if f"{column}{row}" in sheet.cells
    ]
    if not any(value.strip() for value in values):
        return True
    date_coordinate = f"{mapping.fields[FieldName.DATE]}{row}"
    date_cell = sheet.cells.get(date_coordinate)
    date_value = cell_text(date_cell) if date_cell is not None else None
    total_pattern = r"(?:合计|小计|总计|本页合计|累计)\s*[:：]?"
    if re.fullmatch(total_pattern, (date_value or "").strip()):
        return True
    if not date_value and any(re.fullmatch(total_pattern, value.strip()) for value in values):
        return True
    nonempty: list[tuple[str, str]] = []
    for column in mapping.fields.values():
        cell = sheet.cells.get(f"{column}{row}")
        header = sheet.cells.get(f"{column}{mapping.header_row}")
        value = cell_text(cell) if cell is not None else None
        if value:
            nonempty.append((value, cell_text(header) or "" if header is not None else ""))
    return len(nonempty) >= 3 and all(value == header for value, header in nonempty)


class RecordNormalizer:
    """使用已确认规则解析来源，阻塞问题必须显式返回。"""

    def normalize(
        self, snapshots: SourceSnapshots, options: TaskOptions, formula_values: dict[str, str]
    ) -> NormalizationResult:
        """生成 A/B 统一明细及 A 口径合计。

        Args:
            snapshots: 原始来源快照。
            options: 已明确月份及映射的配置。
            formula_values: 有人工审计的公式补充值。

        Returns:
            明细、合计和问题；调用方根据 blocking 决定是否暂停。
        """
        if options.reconciliation_period is None:
            raise ValueError("标准化前必须确认对账年月")
        a_records: list[BusinessRecord] = []
        b_records: list[BusinessRecord] = []
        totals: list[BaselineTotals] = []
        issues: list[Issue] = []
        for mapping in options.mappings:
            workbook = next(
                item for item in snapshots.workbooks if item.source_file.role == mapping.role
            )
            sheet = next(item for item in workbook.sheets if item.name == mapping.sheet_name)
            current_records: list[BusinessRecord] = []
            for row in range(mapping.data_start_row, mapping.data_end_row + 1):
                structural = is_structural_row(sheet, mapping, row)
                if row in mapping.excluded_rows:
                    if not structural:
                        issues.append(
                            Issue(
                                code="BUSINESS_ROW_EXCLUSION",
                                source_ref=f"{sheet.name}!{row}",
                                message="排除行包含业务内容，请缩小到结构行；业务排除应在匹配阶段留原因处理",
                            )
                        )
                    continue
                if structural:
                    continue
                raw: dict[FieldName, CellSnapshot] = {}
                for field, column in mapping.fields.items():
                    cell = (
                        merged_date_cell(sheet, column, row)
                        if field == FieldName.DATE
                        else sheet.cells.get(f"{column}{row}")
                    )
                    if cell is not None:
                        raw[field] = cell
                records = self._records(workbook, sheet, mapping, row, raw, options, formula_values)
                current_records.extend(records)
                issues.extend(issue for record in records for issue in record.issues)
            if mapping.role == FileRole.A:
                a_records.extend(current_records)
                with localcontext() as context:
                    context.prec = 100
                    context.traps[Inexact] = True
                    quantity = sum(
                        (
                            Decimal(item.normalized_fields.quantity or "0")
                            for item in current_records
                        ),
                        Decimal(0),
                    )
                    amount = sum(
                        (Decimal(item.normalized_fields.amount or "0") for item in current_records),
                        Decimal(0),
                    )
                totals.append(
                    BaselineTotals(
                        sheet_name=sheet.name,
                        detail_count=len(current_records),
                        quantity=format(quantity, "f"),
                        amount=format(amount, "f"),
                    )
                )
            else:
                b_records.extend(current_records)
        if not a_records or not b_records:
            issues.append(
                Issue(code="EMPTY_BUSINESS_DATA", message="A/B 表处理范围内都必须存在业务明细")
            )
        return NormalizationResult(
            a_records=tuple(a_records),
            b_records=tuple(b_records),
            totals=tuple(totals),
            issues=tuple(issues),
        )

    def _records(
        self,
        workbook: WorkbookSnapshot,
        sheet: SheetSnapshot,
        mapping: SheetMapping,
        row: int,
        raw: dict[FieldName, CellSnapshot],
        options: TaskOptions,
        formula_values: dict[str, str],
    ) -> tuple[BusinessRecord, ...]:
        """把一个来源行转成 A 明细或 B 的方向记录。

        Args:
            workbook: 原工作簿及日期系统。
            sheet: 原工作表。
            mapping: 当前字段对应关系。
            row: 原始行号。
            raw: 已处理日期合并来源的字段快照。
            options: 期间和名称规则。
            formula_values: 公式人工补充值。

        Returns:
            一个 A 业务记录或一个/多个独立的 B 方向记录。
        """
        issues: list[Issue] = []
        values: dict[FieldName, str | None] = {}
        file_id = workbook.source_file.file_id
        for field in mapping.fields:
            cell = raw.get(field)
            source_ref = f"{file_id}:{sheet.sheet_id}:{cell.coordinate if cell else mapping.fields[field] + str(row)}"
            value = cell_text(cell) if cell is not None else None
            if cell is not None and cell.kind == "formula":
                value = formula_values.get(source_ref, cell.cached_value)
                if value is None:
                    issues.append(
                        Issue(
                            code="FORMULA_RESULT_REQUIRED",
                            message="公式缺少计算结果，请重算来源文件或补充已核实结果",
                            source_ref=source_ref,
                        )
                    )
                if cell.cached_type == "b":
                    issues.append(
                        Issue(
                            code="INVALID_FORMULA_TYPE",
                            message="业务数值公式返回布尔值，请核实来源",
                            source_ref=source_ref,
                        )
                    )
                    value = None
            if cell is not None and cell.kind in {"error", "boolean"}:
                issues.append(
                    Issue(
                        code="INVALID_CELL_TYPE",
                        message="业务字段包含错误值或布尔值",
                        source_ref=source_ref,
                    )
                )
                value = None
            values[field] = value.strip() if value is not None else None
        period = options.reconciliation_period
        if period is None:
            raise ValueError("缺少对账年月")
        parsed_date: str | None = None
        try:
            parsed_date = parse_business_date(
                values.get(FieldName.DATE) or "", period, workbook.date_system
            ).isoformat()
        except ValueError as exc:
            issues.append(
                Issue(
                    code="INVALID_DATE",
                    message=str(exc),
                    source_ref=f"{file_id}:{sheet.sheet_id}:{mapping.fields[FieldName.DATE]}{row}",
                )
            )
        numeric_fields = (
            FieldName.QUANTITY,
            FieldName.UNIT_PRICE,
            FieldName.AMOUNT,
            FieldName.IN_QUANTITY,
            FieldName.IN_PRICE,
            FieldName.IN_AMOUNT,
            FieldName.OUT_QUANTITY,
            FieldName.OUT_PRICE,
            FieldName.OUT_AMOUNT,
            FieldName.FIXED_COST,
        )
        numbers: dict[FieldName, str | None] = {}
        for field in numeric_fields:
            value = values.get(field)
            if value in {None, ""}:
                numbers[field] = None
                continue
            try:
                numbers[field] = format(parse_decimal(value), "f")
            except ValueError as exc:
                numbers[field] = None
                issues.append(
                    Issue(
                        code="INVALID_NUMBER",
                        message=f"{field.value}: {exc}",
                        source_ref=f"{file_id}:{sheet.sheet_id}:{mapping.fields[field]}{row}",
                    )
                )
        row_id = f"{mapping.role.value.upper()}:{file_id}:{sheet.sheet_id}:{row}"
        spec_original = values.get(FieldName.SPEC)
        spec = normalize_label(spec_original) if spec_original else None
        if spec is not None:
            spec = options.specification_aliases.get(spec, spec)
        fields = NormalizedFields(
            date=parsed_date,
            spec=spec,
            quantity=None,
            unit_price=None,
            amount=None,
            document=values.get(FieldName.DOCUMENT),
            product_id=values.get(FieldName.PRODUCT_ID),
            product_name=values.get(FieldName.PRODUCT_NAME),
            warehouse=values.get(FieldName.WAREHOUSE),
            unit=values.get(FieldName.UNIT),
            note=values.get(FieldName.NOTE),
            fixed_cost=numbers.get(FieldName.FIXED_COST),
        )
        source = SourceLocation(
            file_id=file_id,
            sheet_id=sheet.sheet_id,
            sheet_name=sheet.name,
            row=row,
            cells={field: f"{column}{row}" for field, column in mapping.fields.items()},
        )
        if spec is None:
            issues.append(
                Issue(
                    code="MISSING_SPEC",
                    message="规格为空，保留原始行待复核",
                    blocking=False,
                    source_ref=row_id,
                )
            )
        if mapping.role == FileRole.A:
            if parsed_date is not None and not parsed_date.startswith(period):
                issues.append(
                    Issue(
                        code="A_DATE_OUTSIDE_PERIOD",
                        message="A 明细日期不属于任务月份，保留原值待复核",
                        blocking=False,
                        source_ref=row_id,
                    )
                )
            for field in (FieldName.QUANTITY, FieldName.UNIT_PRICE, FieldName.AMOUNT):
                if numbers.get(field) is None:
                    issues.append(
                        Issue(
                            code="REQUIRED_NUMBER_MISSING",
                            message=f"A 表缺少可用的 {field.value}",
                            source_ref=row_id,
                        )
                    )
            quantity = numbers.get(FieldName.QUANTITY)
            direction = (
                Direction.UNKNOWN
                if quantity is None or Decimal(quantity) == 0
                else Direction.IN
                if Decimal(quantity) > 0
                else Direction.OUT
            )
            fields = fields.with_numeric_values(
                quantity, numbers.get(FieldName.UNIT_PRICE), numbers.get(FieldName.AMOUNT)
            )
            return (
                BusinessRecord(
                    record_id=row_id,
                    source=source,
                    raw_fields=raw,
                    normalized_fields=fields,
                    direction=direction,
                    in_period=parsed_date is not None and parsed_date.startswith(period),
                    context={"baseline_row": str(row), "original_spec": spec_original or ""},
                    issues=tuple(issues),
                ),
            )
        result: list[BusinessRecord] = []
        branches = (
            (Direction.IN, FieldName.IN_QUANTITY, FieldName.IN_PRICE, FieldName.IN_AMOUNT),
            (Direction.OUT, FieldName.OUT_QUANTITY, FieldName.OUT_PRICE, FieldName.OUT_AMOUNT),
        )
        active = [
            branch
            for branch in branches
            if any(
                numbers.get(field) is not None and Decimal(numbers[field] or "0") != 0
                for field in branch[1:]
            )
        ]
        if not active:
            populated = [branch for branch in branches if numbers.get(branch[1]) is not None]
            active = populated if len(populated) == 1 else []
        if not active:
            unknown_issue = Issue(
                code="ZERO_OR_UNKNOWN_DIRECTION",
                message="零值或方向无法确定，保留系统记录待复核",
                blocking=False,
                source_ref=row_id,
            )
            zero_fields = fields.with_numeric_values(
                "0"
                if any(
                    numbers.get(field) is not None
                    for field in (FieldName.IN_QUANTITY, FieldName.OUT_QUANTITY)
                )
                else None,
                "0"
                if any(
                    numbers.get(field) is not None
                    for field in (FieldName.IN_PRICE, FieldName.OUT_PRICE)
                )
                else None,
                "0"
                if any(
                    numbers.get(field) is not None
                    for field in (FieldName.IN_AMOUNT, FieldName.OUT_AMOUNT)
                )
                else None,
            )
            return (
                BusinessRecord(
                    record_id=f"{row_id}:unknown",
                    b_row_id=row_id,
                    source=source,
                    raw_fields=raw,
                    normalized_fields=zero_fields,
                    direction=Direction.UNKNOWN,
                    in_period=parsed_date is not None and parsed_date.startswith(period),
                    issues=tuple(issues) + (unknown_issue,),
                ),
            )
        for direction, qty_field, price_field, amount_field in active:
            branch_issues = list(issues)
            for field in (qty_field, price_field, amount_field):
                if numbers.get(field) is None:
                    branch_issues.append(
                        Issue(
                            code="B_NUMERIC_FIELD_EMPTY",
                            message=f"{direction.value} 的 {field.value} 为空，保留待复核",
                            blocking=False,
                            source_ref=row_id,
                        )
                    )
            branch_fields = fields.with_numeric_values(
                numbers.get(qty_field), numbers.get(price_field), numbers.get(amount_field)
            )
            result.append(
                BusinessRecord(
                    record_id=f"{row_id}:{direction.value}",
                    b_row_id=row_id,
                    source=source,
                    raw_fields=raw,
                    normalized_fields=branch_fields,
                    direction=direction,
                    in_period=parsed_date is not None and parsed_date.startswith(period),
                    issues=tuple(branch_issues),
                )
            )
        return tuple(result)


def build_index(records: tuple[BusinessRecord, ...]) -> LookupIndex:
    """建立可重建、保留重复身份的 B 查询索引。

    Args:
        records: 所有月份的 B 方向记录，不能预先筛掉跨月记录。

    Returns:
        按方向、日期、单据、货号、规格及数量查询的索引。
    """
    indices: list[dict[str, list[str]]] = [{} for _ in range(6)]
    for record in records:
        fields = record.normalized_fields
        values = (
            record.direction.value,
            fields.date,
            fields.document,
            fields.product_id,
            fields.spec,
            fields.quantity,
        )
        for index, value in zip(indices, values, strict=True):
            if value is not None:
                index.setdefault(value, []).append(record.record_id)
    frozen = [{key: tuple(values) for key, values in index.items()} for index in indices]
    return LookupIndex(
        by_direction=frozen[0],
        by_date=frozen[1],
        by_document=frozen[2],
        by_product=frozen[3],
        by_spec=frozen[4],
        by_quantity=frozen[5],
    )
