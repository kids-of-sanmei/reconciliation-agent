"""独立重新打开实际文件验收，不把计划、自报使用量或旧公式缓存当作事实。"""

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from fractions import Fraction
from math import ceil
from pathlib import Path
from typing import Literal
from zipfile import ZipFile

from app.delivery.formulas import REFERENCE, Evaluator, FormulaUnsupported
from app.delivery.models import (
    ActualBTrace,
    BCoverage,
    CInspection,
    DeliveryInputs,
    DeliveryOptions,
    DeliveryStatistics,
    GenerationManifest,
    SheetTotals,
    VerificationCheck,
    VerificationIssue,
    VerificationReport,
)
from app.delivery.ooxml import (
    B_HEADERS,
    DIRECTION_LABELS,
    METADATA_HEADERS,
    STATUS_LABELS,
    column,
    column_number,
    display_width,
)
from app.matching.numbers import amount, number, quantity, text
from app.preparation.application.normalization import parse_business_date
from app.preparation.domain.models import (
    CellSnapshot,
    FileRole,
    SheetSnapshot,
    SourceFile,
    WorkbookSnapshot,
)
from app.preparation.domain.values import cell_text
from app.preparation.infrastructure.xlsx import XlsxGateway, parse_xml, tag

FORMULA_ERRORS = {
    "#REF!",
    "#DIV/0!",
    "#VALUE!",
    "#NAME?",
    "#N/A",
    "#NUM!",
    "#SPILL!",
    "#CALC!",
    "#NULL!",
}


def read_output(
    path: Path, role: FileRole, *, max_archive_bytes: int, max_cells: int
) -> WorkbookSnapshot:
    """从磁盘重新读取输出的完整值、公式和结构。

    Args:
        path: 已实际保存的 C 或 B 文件。
        role: 输出对应的原始业务角色。
        max_archive_bytes: 解压部件总字节上限。
        max_cells: 显式单元格上限。

    Returns:
        依据实际字节构建并严格校验的工作簿快照。
    """
    content = path.read_bytes()
    source = SourceFile(
        role=role,
        file_id=f"actual_{role.value}",
        original_name=path.name,
        relative_path=path.name,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )
    return XlsxGateway(max_archive_bytes=max_archive_bytes, max_cells=max_cells).read(path, source)


def value_at(sheet: SheetSnapshot, coordinate: str) -> str | None:
    """读取实际单元格的值或已保存缓存，不把空白变成零。

    Args:
        sheet: 重新打开后的工作表。
        coordinate: 完整单元格坐标。

    Returns:
        实际值；不存在或空白时返回 None。
    """
    cell = sheet.cells.get(coordinate)
    return cell_text(cell) if cell is not None else None


def same_value(actual: CellSnapshot | None, expected: CellSnapshot | None) -> bool:
    """检查来源字段真实值，精确比较数值并逐字比较业务文本。

    Args:
        actual: 输出文件中的真实单元格。
        expected: 不可变来源中的单元格。

    Returns:
        数值未截断或文本未改写时返回 True。
    """
    a = cell_text(actual) if actual else None
    b = cell_text(expected) if expected else None
    if b in {None, ""}:
        return a in {None, ""}
    if expected is not None and expected.kind in {"number", "formula"}:
        try:
            return number(a) == number(b)
        except ValueError:
            return False
    return a == b


def issue(
    code: str,
    message: str,
    *,
    a: str | None = None,
    b: str | None = None,
    location: str | None = None,
    expected: str | None = None,
    actual: str | None = None,
    stage: Literal["matching", "preparation", "input", "delivery"] = "delivery",
) -> VerificationIssue:
    """构建可回流的定位问题，不用自然语言单独驱动完成状态。

    Args:
        code: 稳定错误编号。
        message: 问题意义和需要修复的事实。
        a: 相关 A 来源标识。
        b: 相关 B 来源标识。
        location: 实际输出位置。
        expected: 不可变依据期望值。
        actual: 实际读到的值。
        stage: 修复所属阶段名称。

    Returns:
        带来源标识、定位和值证据的结构化问题。
    """
    target = stage
    return VerificationIssue(
        code=code,
        stage=target,
        message=message,
        a_record_id=a,
        b_record_id=b,
        output_location=location,
        expected=expected,
        actual=actual,
        evidence=tuple(x for x in (a, b, location) if x is not None),
        repairable=target == "delivery",
    )


def expected_positions(
    inputs: DeliveryInputs,
) -> tuple[dict[str, dict[int, int]], dict[str, tuple[str, ...]]]:
    """独立从正式关系推导原始业务主行位置，不读取生成器的行映射结论。

    Args:
        inputs: 不可变来源和已经校验的正式关系。

    Returns:
        各来源行的预期位置，以及应该在每条 A 下实际展开的 B 标识。
    """
    mapping: dict[str, dict[int, int]] = {}
    expected_b: dict[str, tuple[str, ...]] = {}
    seen_groups: set[str] = set()
    by_a = {i: g for g in inputs.matches.groups for i in g.candidate.a_record_ids}
    for record in inputs.a_records:
        group = by_a.get(record.record_id)
        expected_b[record.record_id] = (
            () if group is None or group.group_id in seen_groups else group.candidate.b_record_ids
        )
        if group is not None:
            seen_groups.add(group.group_id)
    for sheet in inputs.baseline.sheets:
        shift = 0
        records = {r.source.row: r for r in inputs.a_records if r.source.sheet_name == sheet.name}
        if not records:
            continue
        rows: dict[int, int] = {}
        for row in range(1, sheet.max_row + 1):
            rows[row] = row + shift
            peer_record = records.get(row)
            if peer_record is not None:
                shift += max(0, len(expected_b[peer_record.record_id]) - 1)
        mapping[sheet.name] = rows
    return mapping, expected_b


class FileVerifier:
    """来源完整性、实际 C 追溯、B 去向和公式显示的独立验收器。"""

    def __init__(self, max_archive_bytes: int, max_cells: int) -> None:
        """设置输出重新读取的资源上限。

        Args:
            max_archive_bytes: 工作簿解压大小限制。
            max_cells: 工作簿显式单元格限制。
        """
        self.max_archive_bytes = max_archive_bytes
        self.max_cells = max_cells

    def read(self, path: Path, role: FileRole) -> WorkbookSnapshot:
        """以生产解析器重新打开磁盘文件。

        Args:
            path: 实际保存路径。
            role: 对应 A/C 或 B 角色。

        Returns:
            真实工作簿快照。
        """
        return read_output(
            path, role, max_archive_bytes=self.max_archive_bytes, max_cells=self.max_cells
        )

    def inspect_c(
        self, path: Path, inputs: DeliveryInputs, manifest: GenerationManifest
    ) -> CInspection:
        """从实际 C 主表和分配表检查来源、唯一性、方向和真实业务量。

        Args:
            path: 已保存的 C 文件。
            inputs: 冻结来源及正式关系。
            manifest: 生成版本的字段契约，行位置另行独立推导。

        Returns:
            通过来源检查的实际 B 追溯事实及全部写入错误。
        """
        workbook = self.read(path, FileRole.A)
        sheets = {s.name: s for s in workbook.sheets}
        positions, expected_b = expected_positions(inputs)
        by_a = {i: g for g in inputs.matches.groups for i in g.candidate.a_record_ids}
        row_results = {r.a_record_id: r for r in inputs.matches.rows}
        b_by_id = {r.record_id: r for r in inputs.b_records}
        a_seen: Counter[str] = Counter()
        b_seen: Counter[str] = Counter()
        issues: list[VerificationIssue] = []
        issues.extend(self._assets(inputs.baseline_path, path))
        traces: list[ActualBTrace] = []
        selected_sheets = {layout.sheet_name for layout in manifest.layouts}
        for original_sheet in inputs.baseline.sheets:
            if original_sheet.name in selected_sheets:
                continue
            preserved = sheets.get(original_sheet.name)
            if preserved is None:
                issues.append(
                    issue(
                        "SOURCE_ATTACHMENT_LOST",
                        "完整原底稿的附件工作表被丢弃",
                        location=original_sheet.name,
                    )
                )
                continue
            for coordinate, original_cell in original_sheet.cells.items():
                preserved_cell = preserved.cells.get(coordinate)
                if original_cell.kind != "formula" and not same_value(
                    preserved_cell, original_cell
                ):
                    issues.append(
                        issue(
                            "SOURCE_ATTACHMENT_CHANGED",
                            "原附件内容发生变化",
                            location=f"{original_sheet.name}!{coordinate}",
                        )
                    )
        for layout in manifest.layouts:
            sheet = sheets.get(layout.sheet_name)
            baseline = next(
                (s for s in inputs.baseline.sheets if s.name == layout.sheet_name), None
            )
            if sheet is None or baseline is None:
                issues.append(
                    issue("C_SHEET_MISSING", "C 表缺少原始业务工作表", location=layout.sheet_name)
                )
                continue
            original_cols = max(
                (column_number(c.column) for c in baseline.cells.values()), default=1
            )
            metadata = {
                name: column(original_cols + i + 1) for i, name in enumerate(METADATA_HEADERS)
            }
            if metadata != layout.metadata or original_cols != layout.original_columns:
                issues.append(
                    issue("INVALID_LAYOUT_CONTRACT", "布局不符合原底稿列范围", location=sheet.name)
                )
            for name, col in metadata.items():
                if value_at(sheet, f"{col}{layout.header_row}") != name:
                    issues.append(
                        issue(
                            "C_HEADER_MISMATCH",
                            "追加字段表头错误",
                            location=f"{sheet.name}!{col}{layout.header_row}",
                            expected=name,
                        )
                    )
            for i, definition in enumerate(layout.trace_columns):
                col = column(original_cols + len(METADATA_HEADERS) + i + 1)
                if value_at(sheet, f"{col}{layout.header_row}") != definition.header:
                    issues.append(
                        issue(
                            "TRACE_HEADER_MISMATCH",
                            "B 追溯表头未按来源承接",
                            location=f"{sheet.name}!{col}{layout.header_row}",
                            expected=definition.header,
                        )
                    )
            for cell in sheet.cells.values():
                if cell.column == metadata["A来源标识"] and cell.row != layout.header_row:
                    identifier = cell_text(cell)
                    if identifier:
                        a_seen[identifier] += 1
                if cell.column == metadata["B来源标识"] and cell.row != layout.header_row:
                    identifier = cell_text(cell)
                    if identifier:
                        b_seen[identifier] += 1
                        if identifier not in b_by_id:
                            issues.append(
                                issue(
                                    "UNKNOWN_B_TRACE",
                                    "实际 C 出现不存在的 B 来源",
                                    b=identifier,
                                    location=f"{sheet.name}!{cell.coordinate}",
                                )
                            )
            # 完整原始底稿的所有已存在单元格都核对，不能仅核对映射后的几列。
            for coordinate, original in baseline.cells.items():
                output_row = positions.get(sheet.name, {}).get(original.row, original.row)
                actual_coordinate = original.column + str(output_row)
                actual_cell = sheet.cells.get(actual_coordinate)
                if original.kind == "formula":
                    if actual_cell is None or actual_cell.kind != "formula":
                        issues.append(
                            issue(
                                "SOURCE_FORMULA_LOST",
                                "原结构公式被丢弃",
                                location=f"{sheet.name}!{actual_coordinate}",
                            )
                        )
                    continue
                if not same_value(actual_cell, original):
                    issues.append(
                        issue(
                            "A_SOURCE_CHANGED",
                            "原始底稿内容被改写或移动错误",
                            location=f"{sheet.name}!{actual_coordinate}",
                            expected=cell_text(original),
                            actual=cell_text(actual_cell) if actual_cell else None,
                        )
                    )
            for record in inputs.a_records:
                if record.source.sheet_name != sheet.name:
                    continue
                output_row = positions[sheet.name][record.source.row]
                group = by_a.get(record.record_id)
                row_result = row_results[record.record_id]
                result_fields = {
                    "A来源标识": record.record_id,
                    "匹配状态": STATUS_LABELS[row_result.status],
                    "匹配组": group.group_id if group else None,
                }
                for field, expected in result_fields.items():
                    if value_at(sheet, f"{metadata[field]}{output_row}") not in (
                        {None, ""} if expected is None else {expected}
                    ):
                        issues.append(
                            issue(
                                "A_RESULT_POSITION",
                                "A 行结果、顺序或匹配组错误",
                                a=record.record_id,
                                location=f"{sheet.name}!{output_row}",
                                expected=expected,
                            )
                        )
                if group is not None:
                    aq = sum(
                        (
                            number(x.quantity)
                            for x in group.candidate.allocations
                            if x.a_record_id == record.record_id
                        ),
                        Fraction(),
                    )
                    am = sum(
                        (
                            number(x.a_amount)
                            for x in group.candidate.allocations
                            if x.a_record_id == record.record_id
                        ),
                        Fraction(),
                    )
                else:
                    aq = am = Fraction()
                numeric_values = {
                    "A已对应数量": text(aq),
                    "A已对应金额": text(am),
                    "A未结数量": text(quantity(record) - aq),
                    "A未结金额": text(amount(record) - am),
                }
                for name, expected in numeric_values.items():
                    actual = value_at(sheet, f"{metadata[name]}{output_row}")
                    if actual != expected:
                        issues.append(
                            issue(
                                "A_ALLOCATION_MISMATCH",
                                "A 的分配或余额与正式关系不一致",
                                a=record.record_id,
                                location=f"{sheet.name}!{metadata[name]}{output_row}",
                                expected=expected,
                                actual=actual,
                            )
                        )
                for offset in range(1, len(expected_b[record.record_id])):
                    for added_col in range(1, original_cols + 1):
                        actual = value_at(sheet, f"{column(added_col)}{output_row + offset}")
                        if actual not in {None, ""}:
                            issues.append(
                                issue(
                                    "ADDED_A_BUSINESS_VALUE",
                                    "展开附加行重复填入 A 原始业务列",
                                    a=record.record_id,
                                    location=f"{sheet.name}!{column(added_col)}{output_row + offset}",
                                    actual=actual,
                                )
                            )
                for offset, identifier in enumerate(expected_b[record.record_id]):
                    current_row = output_row + offset
                    b = b_by_id[identifier]
                    before = len(issues)
                    if group is None:
                        issues.append(
                            issue("MISSING_FORMAL_GROUP", "C 追溯缺少正式匹配组", b=identifier)
                        )
                        continue
                    expected_metadata = {
                        "B来源标识": identifier,
                        "B原始工作表": b.source.sheet_name,
                        "B原始行号": str(b.source.row),
                        "匹配方向": DIRECTION_LABELS[b.direction.value],
                        "匹配组": group.group_id,
                    }
                    for name, expected in expected_metadata.items():
                        actual = value_at(sheet, f"{metadata[name]}{current_row}")
                        if actual != expected:
                            issues.append(
                                issue(
                                    "B_TRACE_REFERENCE",
                                    "C 的 B 来源、方向或组标识错误",
                                    b=identifier,
                                    location=f"{sheet.name}!{metadata[name]}{current_row}",
                                    expected=expected,
                                    actual=actual,
                                )
                            )
                    for i, definition in enumerate(layout.trace_columns):
                        expected_cell = (
                            b.raw_fields.get(definition.field)
                            if b.source.sheet_name in definition.source_sheets
                            else None
                        )
                        if (
                            b.direction.value == "in"
                            and definition.field.value.startswith("out_")
                            or b.direction.value == "out"
                            and definition.field.value.startswith("in_")
                        ):
                            expected_cell = None
                        coordinate = (
                            f"{column(original_cols + len(METADATA_HEADERS) + i + 1)}{current_row}"
                        )
                        if not same_value(sheet.cells.get(coordinate), expected_cell):
                            issues.append(
                                issue(
                                    "B_TRACE_VALUE_CHANGED",
                                    "真实 C 中的 B 原始追溯值缺失或被改写",
                                    b=identifier,
                                    location=f"{sheet.name}!{coordinate}",
                                    expected=cell_text(expected_cell) if expected_cell else None,
                                    actual=value_at(sheet, coordinate),
                                )
                            )
                    bq = sum(
                        (
                            number(x.quantity)
                            for x in group.candidate.allocations
                            if x.b_record_id == identifier
                        ),
                        Fraction(),
                    )
                    bm = sum(
                        (
                            number(x.b_amount)
                            for x in group.candidate.allocations
                            if x.b_record_id == identifier
                        ),
                        Fraction(),
                    )
                    expected_numbers = {
                        "B已对应数量": text(bq),
                        "B已对应金额": text(bm),
                        "B未结数量": text(quantity(b) - bq),
                        "B未结金额": text(amount(b) - bm),
                    }
                    for name, expected in expected_numbers.items():
                        actual = value_at(sheet, f"{metadata[name]}{current_row}")
                        if actual != expected:
                            issues.append(
                                issue(
                                    "B_ALLOCATION_MISMATCH",
                                    "实际 C 中的 B 分配和余额错误",
                                    b=identifier,
                                    location=f"{sheet.name}!{metadata[name]}{current_row}",
                                    expected=expected,
                                    actual=actual,
                                )
                            )
                    if len(issues) == before:
                        traces.append(
                            ActualBTrace(
                                b_record_id=identifier,
                                group_id=group.group_id,
                                c_sheet=sheet.name,
                                c_row=current_row,
                                allocated_quantity=text(bq),
                                allocated_amount=text(bm),
                            )
                        )
        expected_a_ids = {r.record_id for r in inputs.a_records}
        for identifier in expected_a_ids | set(a_seen):
            if a_seen[identifier] != 1 or identifier not in expected_a_ids:
                issues.append(
                    issue(
                        "A_DETAIL_COVERAGE",
                        "A 原始明细存在遗漏、重复或无来源新增",
                        a=identifier,
                        expected="1",
                        actual=str(a_seen[identifier]),
                    )
                )
        expected_b_ids = {i for g in inputs.matches.groups for i in g.candidate.b_record_ids}
        for identifier in expected_b_ids | set(b_seen):
            if b_seen[identifier] != (1 if identifier in expected_b_ids else 0):
                issues.append(
                    issue(
                        "B_TRACE_MULTIPLICITY",
                        "实际 B 追溯的独立来源次数不正确",
                        b=identifier,
                        expected="1" if identifier in expected_b_ids else "0",
                        actual=str(b_seen[identifier]),
                    )
                )
        allocation_sheet = sheets.get(manifest.allocation_sheet)
        expected_edges = Counter(
            (g.group_id, x.a_record_id, x.b_record_id, x.quantity, x.a_amount, x.b_amount)
            for g in inputs.matches.groups
            for x in g.candidate.allocations
        )
        actual_edges: Counter[tuple[str, ...]] = Counter()
        if allocation_sheet is not None:
            for row in range(2, allocation_sheet.max_row + 1):
                if value_at(allocation_sheet, f"A{row}"):
                    actual_edges[
                        tuple(
                            value_at(allocation_sheet, f"{column(col)}{row}") or ""
                            for col in range(1, 7)
                        )
                    ] += 1
        if expected_edges != actual_edges:
            issues.append(
                issue(
                    "ALLOCATION_DETAIL_MISMATCH",
                    "实际分配明细缺失、重复或超出正式关系",
                    location=manifest.allocation_sheet,
                )
            )
        audit_sheet = sheets.get(manifest.audit_sheet)
        expected_records = {r.record_id: r for r in (*inputs.a_records, *inputs.b_records)}
        audited: Counter[str] = Counter()
        if audit_sheet is not None:
            for row in range(2, audit_sheet.max_row + 1):
                identifier = value_at(audit_sheet, f"B{row}")
                audit_record = expected_records.get(identifier or "")
                if audit_record is None:
                    issues.append(
                        issue(
                            "SOURCE_AUDIT_REFERENCE",
                            "来源审计出现无来源记录",
                            location=f"{manifest.audit_sheet}!B{row}",
                        )
                    )
                    continue
                audited[audit_record.record_id] += 1
                expected_json = json.dumps(
                    {
                        f.value: {
                            "cell": c.coordinate,
                            "value": c.value,
                            "formula": c.formula,
                            "cached": c.cached_value,
                        }
                        for f, c in audit_record.raw_fields.items()
                    },
                    ensure_ascii=False,
                )
                if value_at(audit_sheet, f"H{row}") != expected_json:
                    issues.append(
                        issue(
                            "SOURCE_AUDIT_VALUE",
                            "审计中的原值或原公式丢失/改写",
                            location=f"{manifest.audit_sheet}!H{row}",
                        )
                    )
        if any(audited[identifier] != 1 for identifier in expected_records):
            issues.append(
                issue(
                    "SOURCE_AUDIT_COVERAGE",
                    "来源审计没有完整保留 A/B 独立记录和原公式",
                    location=manifest.audit_sheet,
                )
            )
        valid = tuple(t for t in traces if not any(i.b_record_id == t.b_record_id for i in issues))
        codes = {i.code for i in issues}
        checks = (
            VerificationCheck(
                name="a_integrity",
                status="fail"
                if any(
                    c.startswith("A_")
                    or c
                    in {
                        "C_SHEET_MISSING",
                        "SOURCE_FORMULA_LOST",
                        "ADDED_A_BUSINESS_VALUE",
                        "INVALID_LAYOUT_CONTRACT",
                    }
                    for c in codes
                )
                else "pass",
                detail="独立定位全部 A 原始业务及原底稿内容，附加行不重复业务值",
            ),
            VerificationCheck(
                name="b_trace",
                status="fail"
                if any(
                    c.startswith("B_") or c in {"TRACE_HEADER_MISMATCH", "C_HEADER_MISMATCH"}
                    for c in codes
                )
                else "pass",
                detail="从真实 C 单元格核对 B 身份、原始字段、方向、精度和独立出现次数",
            ),
            VerificationCheck(
                name="allocations",
                status="fail" if any("ALLOCATION" in c for c in codes) else "pass",
                detail="真实分配表与主表余额一致，不按原始追溯展示次数重复累计",
            ),
        )
        return CInspection(traces=valid, issues=tuple(issues), checks=checks)

    def _assets(self, source_path: Path, actual_path: Path) -> tuple[VerificationIssue, ...]:
        """核对完整复制的图片、嵌入对象及其他不可重建资产。

        Args:
            source_path: 不可变的原包或完整底稿。
            actual_path: 实际 C/B 输出包。

        Returns:
            原始二进制或自定义 XML 资产缺失/变化的问题。
        """
        problems: list[VerificationIssue] = []
        with ZipFile(source_path) as source, ZipFile(actual_path) as actual:
            names = set(actual.namelist())
            for name in source.namelist():
                if not (
                    name.startswith(("xl/media/", "xl/embeddings/", "customXml/"))
                    or name.endswith(".bin")
                ):
                    continue
                if name not in names or actual.read(name) != source.read(name):
                    problems.append(
                        issue(
                            "SOURCE_ASSET_CHANGED", "完整原始工作簿的资产缺失或变化", location=name
                        )
                    )
        return tuple(problems)

    def aggregate_scopes(
        self, actual: WorkbookSnapshot, inputs: DeliveryInputs
    ) -> tuple[VerificationIssue, ...]:
        """独立核对每个数量/金额小计的业务覆盖和实际结果，旧范围不能靠新缓存通过。

        Args:
            actual: 重新读取的实际 C 文件。
            inputs: 原底稿公式和不可变业务明细。

        Returns:
            汇总范围遗漏、重复累计或结果与应覆盖业务不符的问题。
        """
        positions, _ = expected_positions(inputs)
        original_cells = {
            (s.name, c.coordinate): c for s in inputs.baseline.sheets for c in s.cells.values()
        }
        actual_cells = {(s.name, c.coordinate): c for s in actual.sheets for c in s.cells.values()}
        original_business: dict[tuple[str, str], tuple[str, str]] = {}
        actual_business: dict[tuple[str, str], tuple[str, str]] = {}
        business_values: dict[tuple[str, str], Fraction] = {}
        for record in inputs.a_records:
            for field in record.source.cells:
                if field.value not in {"quantity", "amount"}:
                    continue
                original_coordinate = record.source.cells[field]
                key = (record.record_id, field.value)
                original_business[(record.source.sheet_name, original_coordinate)] = key
                actual_business[
                    (
                        record.source.sheet_name,
                        original_coordinate.rstrip("0123456789")
                        + str(positions[record.source.sheet_name][record.source.row]),
                    )
                ] = key
                business_values[key] = number(
                    record.normalized_fields.quantity
                    if field.value == "quantity"
                    else record.normalized_fields.amount
                )

        def dependencies(
            key: tuple[str, str],
            cells: dict[tuple[str, str], CellSnapshot],
            business: dict[tuple[str, str], tuple[str, str]],
            visiting: set[tuple[str, str]],
        ) -> Counter[tuple[str, str]]:
            """递归展开实际公式引用，记录业务单元格的重复次数。

            Args:
                key: 当前工作表及坐标。
                cells: 原包或实际输出的真实单元格。
                business: 当前坐标系下的原始业务身份。
                visiting: 当前递归路径，用于检测循环引用。

            Returns:
                被引用业务身份及其累计次数。
            """
            if key in business:
                return Counter({business[key]: 1})
            cell = cells.get(key)
            if cell is None or cell.kind != "formula":
                return Counter()
            if key in visiting:
                raise FormulaUnsupported("汇总公式存在循环")
            visiting.add(key)
            formula = cell.formula or ""
            if not re.match(r"^(SUM|SUBTOTAL|VALUE)\s*\(", formula, re.IGNORECASE):
                raise FormulaUnsupported("汇总依赖包含当前范围验收不支持的表达式")
            references = list(REFERENCE.finditer(formula))
            result: Counter[tuple[str, str]] = Counter()
            index = 0
            while index < len(references):
                ref = references[index]
                name = (ref.group("sheet") or key[0]).strip("'").replace("''", "'")
                start_col = column_number(ref.group("col").lstrip("$"))
                start_row = int(ref.group("row").lstrip("$"))
                end_col, end_row = start_col, start_row
                if (
                    index + 1 < len(references)
                    and formula[ref.end() : references[index + 1].start()].strip() == ":"
                ):
                    other = references[index + 1]
                    end_col = column_number(other.group("col").lstrip("$"))
                    end_row = int(other.group("row").lstrip("$"))
                    index += 1
                if (end_col - start_col + 1) * (end_row - start_row + 1) > 500000:
                    raise FormulaUnsupported("汇总范围超过预算")
                for row in range(start_row, end_row + 1):
                    for col in range(start_col, end_col + 1):
                        child = (name, f"{column(col)}{row}")
                        nested = cells.get(child)
                        if (
                            formula.upper().startswith("SUBTOTAL")
                            and nested
                            and nested.formula
                            and nested.formula.upper().startswith("SUBTOTAL")
                        ):
                            continue
                        result.update(dependencies(child, cells, business, visiting))
                index += 1
            visiting.remove(key)
            return result

        problems: list[VerificationIssue] = []
        for (sheet, coordinate), source in original_cells.items():
            if (
                source.kind != "formula"
                or sheet not in positions
                or not re.match(r"^(SUM|SUBTOTAL)\s*\(", source.formula or "", re.IGNORECASE)
            ):
                continue
            if not any(
                name == sheet and ref.rstrip("0123456789") == coordinate.rstrip("0123456789")
                for name, ref in original_business
            ):
                continue
            output_coordinate = coordinate.rstrip("0123456789") + str(positions[sheet][source.row])
            try:
                expected = set(
                    dependencies((sheet, coordinate), original_cells, original_business, set())
                )
                observed = dependencies(
                    (sheet, output_coordinate), actual_cells, actual_business, set()
                )
                if expected != set(observed) or any(count != 1 for count in observed.values()):
                    problems.append(
                        issue(
                            "TOTAL_RANGE_INCOMPLETE",
                            "合计公式遗漏业务或重复累计小计，缓存正确也不能替代范围验收",
                            location=f"{sheet}!{output_coordinate}",
                        )
                    )
                actual_value = cell_text(actual_cells[(sheet, output_coordinate)])
                expected_value = sum((business_values[k] for k in expected), Fraction())
                if expected and number(actual_value) != expected_value:
                    problems.append(
                        issue(
                            "TOTAL_VALUE_MISMATCH",
                            "真实汇总公式结果不等于应覆盖的业务合计",
                            location=f"{sheet}!{output_coordinate}",
                            expected=text(expected_value),
                            actual=actual_value,
                        )
                    )
            except (ValueError, KeyError) as exc:
                problems.append(
                    issue(
                        "TOTAL_SCOPE_UNVERIFIED",
                        str(exc),
                        location=f"{sheet}!{output_coordinate}",
                        stage="input" if isinstance(exc, FormulaUnsupported) else "delivery",
                    )
                )
        return tuple(problems)

    def coverage(self, inputs: DeliveryInputs, inspection: CInspection) -> tuple[BCoverage, ...]:
        """以重新核实的 B 系统月份和实际 C 承接事实决定互斥去向。

        Args:
            inputs: 不可变 B 来源和人工排除。
            inspection: 已重新读取并核对的实际 C 事实。

        Returns:
            每条方向业务记录的去向、明确事实说明和精确余额。
        """
        period = inputs.prepared.task_context.reconciliation_period
        books = {w.source_file.file_id: w for w in inputs.snapshots.workbooks}
        actual = {t.b_record_id: t for t in inspection.traces}
        excluded = {r.b_record_id: r for r in inputs.matches.exclusions}
        by_b = {i: g for g in inputs.matches.groups for i in g.candidate.b_record_ids}
        result: list[BCoverage] = []
        remaining_q: str | None
        remaining_m: str | None
        for b in inputs.b_records:
            raw_date = (
                b.raw_fields.get(next(f for f in b.source.cells if f.value == "date"))
                if any(f.value == "date" for f in b.source.cells)
                else None
            )
            date_value = cell_text(raw_date) if raw_date else None
            known_period = True
            try:
                parsed_date = parse_business_date(
                    date_value or "", period, books[b.source.file_id].date_system
                ).isoformat()
                in_period = parsed_date.startswith(period)
            except ValueError:
                known_period = False
                in_period = False
            trace = actual.get(b.record_id)
            allocated_q = number(trace.allocated_quantity) if trace else Fraction()
            allocated_m = number(trace.allocated_amount) if trace else Fraction()
            try:
                remaining_q, remaining_m = (
                    text(quantity(b) - allocated_q),
                    text(amount(b) - allocated_m),
                )
            except ValueError:
                remaining_q = remaining_m = None
            exclusion = excluded.get(b.record_id)
            missing_type = ""
            disposition: Literal["taken", "missing", "excluded", "outside_period", "unknown_period"]
            if not known_period:
                disposition = "unknown_period"
                explanation = "B 原始系统日期无法确定月份，需先修正来源或字段映射"
            elif not in_period:
                disposition = "outside_period"
                explanation = "非本月系统记录，不纳入本月缺失统计" + (
                    "；按已确认日期范围保留跨月追溯" if trace else ""
                )
            elif exclusion is not None:
                disposition = "excluded"
                explanation = "经人工确认业务排除：" + exclusion.reason
            elif trace is not None and remaining_q == "0" and remaining_m == "0":
                disposition = "taken"
                explanation = "实际 C 中存在来源和精度正确的追溯，业务量已完整承接"
            else:
                disposition = "missing"
                if trace is not None:
                    missing_type = "部分未承接"
                    explanation = "本月系统记录仅部分承接到 C 表，剩余业务量已明确列出"
                else:
                    missing_type = (
                        "入库缺失"
                        if b.direction.value == "in"
                        else "出库缺失"
                        if b.direction.value == "out"
                        else "方向未确定"
                    )
                    if b.normalized_fields.quantity is None:
                        missing_type = "空数量"
                    elif (
                        b.normalized_fields.amount is not None
                        and number(b.normalized_fields.amount) == 0
                    ):
                        missing_type = "零金额缺失"
                    explanation = "B表本月记录未在C表中找到通过来源核查的承接"
                    if any(i.b_record_id == b.record_id for i in inspection.issues):
                        explanation += "；实际 C 存在写入错误，详见核查报告，不能正式交付"
            group = by_b.get(b.record_id)
            result.append(
                BCoverage(
                    b_record_id=b.record_id,
                    source_sheet=b.source.sheet_name,
                    source_row=b.source.row,
                    direction=b.direction.value,
                    in_period=in_period,
                    disposition=disposition,
                    missing_type=missing_type,
                    explanation=explanation,
                    allocated_quantity=text(allocated_q),
                    allocated_amount=text(allocated_m),
                    remaining_quantity=remaining_q,
                    remaining_amount=remaining_m,
                    c_references=(f"{trace.c_sheet}!{trace.c_row}",) if trace else (),
                    a_record_ids=group.candidate.a_record_ids if group else (),
                    group_ids=(group.group_id,) if group else (),
                    exclusion_reason=exclusion.reason if exclusion else None,
                    confirmation_ref=exclusion.confirmation_ref if exclusion else None,
                )
            )
        return tuple(result)

    def totals(
        self, c: WorkbookSnapshot, inputs: DeliveryInputs
    ) -> tuple[tuple[SheetTotals, ...], tuple[VerificationIssue, ...]]:
        """独立读取 C 原业务单元格重新累计 A/C 合计。

        Args:
            c: 实际 C 快照。
            inputs: 不可变 A 明细和业务数量金额口径。

        Returns:
            分工作表的精确合计、零差额或定位错误。
        """
        positions, _ = expected_positions(inputs)
        results: list[SheetTotals] = []
        issues: list[VerificationIssue] = []
        for sheet_name, mapping in positions.items():
            sheet = next((s for s in c.sheets if s.name == sheet_name), None)
            records = tuple(r for r in inputs.a_records if r.source.sheet_name == sheet_name)
            aq = sum((number(r.normalized_fields.quantity) for r in records), Fraction())
            am = sum((number(r.normalized_fields.amount) for r in records), Fraction())
            cq = cm = Fraction()
            for r in records:
                for field_name in ("quantity", "amount"):
                    field = next(f for f in r.source.cells if f.value == field_name)
                    coordinate = r.source.cells[field].rstrip("0123456789") + str(
                        mapping[r.source.row]
                    )
                    raw = value_at(sheet, coordinate) if sheet else None
                    try:
                        from app.preparation.application.normalization import parse_decimal

                        parsed = Fraction(parse_decimal(raw or ""))
                        if field_name == "quantity":
                            cq += parsed
                        else:
                            cm += parsed
                    except ValueError:
                        issues.append(
                            issue(
                                "A_TOTAL_NUMBER_MISSING",
                                "实际 C 业务数值缺失或无法计算",
                                a=r.record_id,
                                location=f"{sheet_name}!{coordinate}",
                                actual=raw,
                            )
                        )
            results.append(
                SheetTotals(
                    sheet_name=sheet_name,
                    a_quantity=text(aq),
                    c_quantity=text(cq),
                    quantity_difference=text(cq - aq),
                    a_amount=text(am),
                    c_amount=text(cm),
                    amount_difference=text(cm - am),
                )
            )
            if aq != cq or am != cm:
                issues.append(
                    issue(
                        "A_C_TOTAL_MISMATCH",
                        "A/C 原始业务合计差额不为零",
                        location=sheet_name,
                        expected=f"{text(aq)} / {text(am)}",
                        actual=f"{text(cq)} / {text(cm)}",
                    )
                )
        return tuple(results), tuple(issues)

    def verify(
        self,
        c_path: Path,
        b_path: Path,
        inputs: DeliveryInputs,
        manifest: GenerationManifest,
        options: DeliveryOptions,
    ) -> VerificationReport:
        """重新读取全部输出，只有强制项全部通过才形成通过报告。

        Args:
            c_path: 实际 C 文件。
            b_path: 实际标注 B 文件。
            inputs: 冻结来源、期间和正式匹配版本。
            manifest: 本次生成的版本及布局契约。
            options: 明确的公式、显示能力配置。

        Returns:
            真实检查、互斥覆盖、统计和可回流问题组成的核查报告。
        """
        inspection = self.inspect_c(c_path, inputs, manifest)
        coverage = self.coverage(inputs, inspection)
        c, b = self.read(c_path, FileRole.A), self.read(b_path, FileRole.B)
        totals, total_issues = self.totals(c, inputs)
        scope_issues = self.aggregate_scopes(c, inputs)
        issues = [
            *inspection.issues,
            *total_issues,
            *scope_issues,
            *self._assets(inputs.b_source_path, b_path),
        ]
        checks = [
            *inspection.checks,
            VerificationCheck(
                name="a_c_totals",
                status="fail" if total_issues else "pass",
                detail="重新累计真实 C 的 A 原业务列，数量与金额差额均须为零",
            ),
        ]
        checks.append(
            VerificationCheck(
                name="aggregate_formulas",
                status="not_run"
                if any(i.stage == "input" for i in scope_issues)
                else "fail"
                if scope_issues
                else "pass",
                detail="逐个小计/合计展开真实引用，检查业务覆盖、重复次数及实际结果，不只比较缓存",
            )
        )
        b_issues = self._verify_b(b, inputs, coverage)
        issues.extend(b_issues)
        checks.append(
            VerificationCheck(
                name="b_original_and_annotation",
                status="fail" if b_issues else "pass",
                detail="原始 B 数据和顺序未改写，每条方向记录均有实际标注",
            )
        )
        unknown = tuple(r for r in coverage if r.disposition == "unknown_period")
        for record in unknown:
            issues.append(
                issue(
                    "UNKNOWN_B_PERIOD",
                    record.explanation,
                    b=record.b_record_id,
                    stage="preparation",
                )
            )
        monthly = tuple(r for r in coverage if r.in_period)
        counts = Counter(r.disposition for r in monthly)
        coverage_passed = (
            not unknown and len(monthly) == counts["taken"] + counts["missing"] + counts["excluded"]
        )
        checks.append(
            VerificationCheck(
                name="b_monthly_coverage",
                status="pass" if coverage_passed else "fail",
                detail="以 B 原始系统日期重判月份；已承接、缺失、人工排除互斥，方向记录和原始行分别统计",
            )
        )
        for path, snapshot, prefix in ((c_path, c, "c"), (b_path, b, "b")):
            formula_checks, formula_issues = self._formulas(path, snapshot, options, prefix)
            checks.extend(formula_checks)
            issues.extend(formula_issues)
            display_checks, display_issues = self._display(
                path, snapshot, manifest, options, prefix
            )
            checks.extend(display_checks)
            issues.extend(display_issues)
        statistics = DeliveryStatistics(
            a_detail_count=len(inputs.a_records),
            b_current_month_original_rows=len({(r.source_sheet, r.source_row) for r in monthly}),
            b_current_month_business_records=len(monthly),
            taken_business_records=counts["taken"],
            missing_business_records=counts["missing"],
            excluded_business_records=counts["excluded"],
            outside_period_business_records=sum(
                r.disposition == "outside_period" for r in coverage
            ),
            unknown_period_business_records=len(unknown),
            row_status_counts=dict(Counter(r.status for r in inputs.matches.rows)),
            totals=totals,
        )
        return VerificationReport(
            task_id=inputs.plan.task_id,
            data_version=inputs.plan.prepared_data_version,
            match_plan_version=inputs.plan.match_plan_version,
            generation_version=manifest.generation_version,
            verified_at=datetime.now(UTC),
            checks=tuple(checks),
            issues=tuple(issues),
            coverage=coverage,
            statistics=statistics,
            formula_engine="exact-formulas-v1"
            if options.formula_engine == "builtin"
            else "unavailable",
            display_engine="ooxml-display-bounds-v1"
            if options.display_engine == "bounds"
            else "unavailable",
            passed=not issues and all(c.status == "pass" for c in checks if c.mandatory),
        )

    def _verify_b(
        self, actual: WorkbookSnapshot, inputs: DeliveryInputs, coverage: tuple[BCoverage, ...]
    ) -> tuple[VerificationIssue, ...]:
        """对照原始 B 文件及实际 C 事实核对每一条副本标注。

        Args:
            actual: 重读的标注 B 副本。
            inputs: 不可变 B 来源。
            coverage: 从真实 C 得到的记录去向。

        Returns:
            原值变化、缺失标注或排除依据错误。
        """
        from app.delivery.mark_b import annotation_values

        original = next(w for w in inputs.snapshots.workbooks if w.source_file.role == FileRole.B)
        actual_sheets = {s.name: s for s in actual.sheets}
        issues: list[VerificationIssue] = []
        mappings = {
            m.sheet_name: m
            for m in inputs.prepared.task_context.field_mappings
            if m.role == FileRole.B
        }
        by_source_row: dict[tuple[str, int], list[BCoverage]] = defaultdict(list)
        for record in coverage:
            by_source_row[(record.source_sheet, record.source_row)].append(record)
        for source in original.sheets:
            sheet = actual_sheets.get(source.name)
            if sheet is None:
                issues.append(issue("B_SOURCE_SHEET_LOST", "B 原工作表缺失", location=source.name))
                continue
            for coordinate, cell in source.cells.items():
                observed = sheet.cells.get(coordinate)
                valid = (
                    (observed is not None and observed.formula == cell.formula)
                    if cell.kind == "formula"
                    else same_value(observed, cell)
                )
                if not valid:
                    issues.append(
                        issue(
                            "B_SOURCE_CHANGED",
                            "B 副本改写或移动了原始数据",
                            location=f"{source.name}!{coordinate}",
                            expected=cell_text(cell),
                            actual=cell_text(observed) if observed else None,
                        )
                    )
            mapping = mappings.get(source.name)
            if mapping is None:
                continue
            original_cols = max((column_number(c.column) for c in source.cells.values()), default=1)
            for i, header in enumerate(B_HEADERS):
                if (
                    value_at(sheet, f"{column(original_cols + i + 1)}{mapping.header_row}")
                    != header
                ):
                    issues.append(
                        issue(
                            "B_ANNOTATION_HEADER",
                            "B 标注表头不完整",
                            location=source.name,
                            expected=header,
                        )
                    )
            rows = {r.source_row for r in coverage if r.source_sheet == source.name}
            for row in rows:
                records = tuple(by_source_row[(source.name, row)])
                status_value = value_at(sheet, f"{column(original_cols + 1)}{row}") or ""
                semantic_labels = {
                    "taken": "已承接",
                    "missing": "C表缺失",
                    "excluded": "人工排除",
                    "outside_period": "非本月",
                    "unknown_period": "月份待确认",
                }
                for record in records:
                    expected_status = semantic_labels[record.disposition]
                    expected_fragment = (
                        expected_status
                        if len(records) == 1
                        else f"{DIRECTION_LABELS[record.direction]}：{expected_status}"
                    )
                    if (len(records) == 1 and status_value != expected_status) or (
                        len(records) > 1 and expected_fragment not in status_value.split(" | ")
                    ):
                        issues.append(
                            issue(
                                "B_DISPOSITION_FALSE",
                                "实际 B 去向标注与独立反向核查事实冲突",
                                b=record.b_record_id,
                                location=f"{source.name}!{column(original_cols + 1)}{row}",
                                expected=expected_fragment,
                                actual=status_value,
                            )
                        )
                    if len(records) == 1:
                        for offset, balance in (
                            (4, record.remaining_quantity),
                            (5, record.remaining_amount),
                        ):
                            if (
                                value_at(sheet, f"{column(original_cols + offset)}{row}") or ""
                            ) != (balance if balance is not None else "不可计算"):
                                issues.append(
                                    issue(
                                        "B_BALANCE_FALSE",
                                        "B 余额标注不等于原始量减实际承接量",
                                        b=record.b_record_id,
                                        location=f"{source.name}!{row}",
                                    )
                                )
                expected_values = annotation_values(records)
                for i, expected in enumerate(expected_values):
                    actual_value = value_at(sheet, f"{column(original_cols + i + 1)}{row}") or ""
                    if actual_value != expected:
                        issues.append(
                            issue(
                                "B_ANNOTATION_MISMATCH",
                                "B 真实标注与实际 C 核查结果不一致",
                                b=records[0].b_record_id,
                                location=f"{source.name}!{column(original_cols + i + 1)}{row}",
                                expected=expected,
                                actual=actual_value,
                            )
                        )
        return tuple(issues)

    def _formulas(
        self, path: Path, workbook: WorkbookSnapshot, options: DeliveryOptions, prefix: str
    ) -> tuple[tuple[VerificationCheck, ...], tuple[VerificationIssue, ...]]:
        """不依赖已存缓存重新计算公式，并扫描明确错误值。

        Args:
            path: 实际工作簿路径。
            workbook: 实际值和公式快照。
            options: 公式引擎能力设置。
            prefix: C 或 B 检查名前缀。

        Returns:
            重算、缓存、错误检查及能力不足的定位问题。
        """
        issues: list[VerificationIssue] = []
        for sheet in workbook.sheets:
            for cell in sheet.cells.values():
                value = cell_text(cell)
                if (
                    cell.kind == "error"
                    or cell.cached_type == "e"
                    or value in FORMULA_ERRORS
                    or cell.formula
                    and any(error in cell.formula for error in FORMULA_ERRORS)
                ):
                    issues.append(
                        issue(
                            "FORMULA_ERROR",
                            "实际文件存在公式错误值",
                            location=f"{sheet.name}!{cell.coordinate}",
                            actual=value or cell.formula,
                        )
                    )
        if options.formula_engine == "unavailable":
            return (
                VerificationCheck(
                    name=f"{prefix}_formula_recalculation",
                    status="not_run",
                    detail="公式重算能力未启用，不能以缓存代替验收",
                ),
            ), tuple(issues)
        with ZipFile(path) as archive:
            roots = {s.name: parse_xml(archive.read(s.xml_path)) for s in workbook.sheets}
            strings = (
                tuple(
                    "".join(n.text or "" for n in item.iter(tag("t")))
                    for item in parse_xml(archive.read("xl/sharedStrings.xml"))
                )
                if "xl/sharedStrings.xml" in archive.namelist()
                else ()
            )
        evaluator = Evaluator(roots, strings)
        unsupported = False
        for sheet in workbook.sheets:
            for cell in sheet.cells.values():
                if cell.kind != "formula":
                    continue
                try:
                    calculated = evaluator.value(sheet.name, cell.coordinate)
                    if cell.cached_value is None or number(cell.cached_value) != calculated:
                        issues.append(
                            issue(
                                "FORMULA_CACHE_MISMATCH",
                                "实际公式缓存缺失或与独立重算结果不一致",
                                location=f"{sheet.name}!{cell.coordinate}",
                                expected=text(calculated),
                                actual=cell.cached_value,
                            )
                        )
                except FormulaUnsupported as exc:
                    unsupported = True
                    issues.append(
                        issue(
                            "FORMULA_NOT_SUPPORTED",
                            str(exc),
                            location=f"{sheet.name}!{cell.coordinate}",
                            stage="input",
                        )
                    )
                except (ValueError, ZeroDivisionError) as exc:
                    issues.append(
                        issue(
                            "FORMULA_CALCULATION_FAILED",
                            str(exc),
                            location=f"{sheet.name}!{cell.coordinate}",
                        )
                    )
        status: Literal["not_run", "fail", "pass"] = (
            "not_run" if unsupported else "fail" if issues else "pass"
        )
        return (
            VerificationCheck(
                name=f"{prefix}_formula_recalculation",
                status=status,
                detail="支持 SUM、SUBTOTAL(9/109)、VALUE、加减乘除和跨表引用；所有实际公式须重新计算且缓存一致",
            ),
        ), tuple(issues)

    def _display(
        self,
        path: Path,
        workbook: WorkbookSnapshot,
        manifest: GenerationManifest,
        options: DeliveryOptions,
        prefix: str,
    ) -> tuple[tuple[VerificationCheck, ...], tuple[VerificationIssue, ...]]:
        """验证受支持 OOXML 显示约束，未知格式和超限值明确不宣称通过。

        Args:
            path: 实际输出文件。
            workbook: 重新读取的实际数值、格式及尺寸。
            manifest: 需要冻结和复位的业务/审计表定义。
            options: 显示检查能力开关。
            prefix: C 或 B 检查名前缀。

        Returns:
            显示、视图检查，以及无法证实可读性的明确问题。
        """
        if options.display_engine == "unavailable":
            return (
                VerificationCheck(
                    name=f"{prefix}_display",
                    status="not_run",
                    detail="没有启用受支持的显示验证能力",
                ),
            ), ()
        issues: list[VerificationIssue] = []
        unsupported = False
        required = {l.sheet_name: l.header_row for l in manifest.layouts} if prefix == "c" else {}
        if prefix == "c":
            required.update(
                {manifest.audit_sheet: 1, manifest.allocation_sheet: 1, manifest.review_sheet: 1}
            )
        with ZipFile(path) as archive:
            style_root = parse_xml(archive.read("xl/styles.xml"))
            fonts = style_root.find(tag("fonts"))
            styles = style_root.find(tag("cellXfs"))
            for sheet in workbook.sheets:
                root = parse_xml(archive.read(sheet.xml_path))
                if prefix == "b" and any(
                    value_at(sheet, f"{c.column}{c.row}") == "C表缺失标注"
                    for c in sheet.cells.values()
                ):
                    header = next(
                        c.row for c in sheet.cells.values() if cell_text(c) == "C表缺失标注"
                    )
                    required[sheet.name] = header
                if sheet.name not in required:
                    continue
                view = root.find(f"{tag('sheetViews')}/{tag('sheetView')}")
                pane = None if view is None else view.find(tag("pane"))
                selections = [] if view is None else view.findall(tag("selection"))
                if (
                    view is None
                    or view.get("topLeftCell") != "A1"
                    or pane is None
                    or pane.get("ySplit") != str(required[sheet.name])
                    or not selections
                    or any(s.get("activeCell") != "A1" for s in selections)
                ):
                    issues.append(
                        issue(
                            "OUTPUT_VIEW_INVALID",
                            "打开位置或冻结位置不符合实际表头",
                            location=sheet.name,
                        )
                    )
                if root.find(tag("autoFilter")) is None:
                    issues.append(
                        issue("OUTPUT_FILTER_MISSING", "业务表缺少筛选范围", location=sheet.name)
                    )
                if any(
                    root.find(tag(name)) is not None
                    for name in ("tableParts", "extLst", "drawing", "legacyDrawing", "oleObjects")
                ):
                    unsupported = True
                    issues.append(
                        issue(
                            "DISPLAY_FEATURE_UNVERIFIED",
                            "原生表格、绘图对象或扩展显示超出当前显示边界引擎，需受支持的渲染能力验收",
                            location=sheet.name,
                            stage="input",
                        )
                    )
                row_nodes = {
                    int(row.get("r", "0")): row
                    for row in root.findall(f"{tag('sheetData')}/{tag('row')}")
                }
                xml_cells = {c.get("r", ""): c for c in root.iter(tag("c"))}
                widths: dict[int, Fraction] = {}
                for col in root.findall(f"{tag('cols')}/{tag('col')}"):
                    width = number(col.get("width", "0"))
                    for index in range(int(col.get("min", "0")), int(col.get("max", "0")) + 1):
                        widths[index] = Fraction() if col.get("hidden") == "1" else width
                for cell in sheet.cells.values():
                    value = cell_text(cell)
                    if value in {None, ""}:
                        continue
                    format_code = cell.number_format
                    numeric = (
                        cell.kind == "number"
                        or cell.kind == "formula"
                        and cell.cached_type not in {"str", "s", "inlineStr"}
                    )
                    date_format = format_code == "yyyy-mm-dd hh:mm:ss"
                    if numeric:
                        if format_code not in {"General", "0", "yyyy-mm-dd hh:mm:ss"}:
                            unsupported = True
                            issues.append(
                                issue(
                                    "DISPLAY_FORMAT_UNVERIFIED",
                                    "数字格式不在已验证能力内，不能保证精度或整数尾点",
                                    location=f"{sheet.name}!{cell.coordinate}",
                                    actual=format_code,
                                    stage="input",
                                )
                            )
                            continue
                        try:
                            parsed = number(value)
                            display = text(parsed)
                            significant = (
                                display.lstrip("-0").replace(".", "").lstrip("0").rstrip("0")
                            )
                            if (
                                (not date_format and len(significant) > 15)
                                or format_code == "0"
                                and parsed.denominator != 1
                            ):
                                unsupported = True
                                issues.append(
                                    issue(
                                        "DISPLAY_PRECISION_UNVERIFIED",
                                        "源值保留，但超过当前 Excel 数值显示精度的可验证范围",
                                        location=f"{sheet.name}!{cell.coordinate}",
                                        actual=value,
                                        stage="input",
                                    )
                                )
                            if date_format:
                                display = "2026-12-31 23:59:59"
                        except ValueError:
                            display = value
                    else:
                        display = value
                    width = widths.get(column_number(cell.column), Fraction())
                    font_size = Fraction(11)
                    if fonts is not None and styles is not None:
                        font = fonts[int(styles[cell.style_id].get("fontId", "0"))]
                        size_node = font.find(tag("sz"))
                        font_size = (
                            number(size_node.get("val", "11"))
                            if size_node is not None
                            else Fraction(11)
                        )
                        alignment = styles[cell.style_id].find(tag("alignment"))
                        xml_cell = xml_cells.get(cell.coordinate)
                        has_rich_font = xml_cell is not None and any(
                            n.tag == tag("rPr") for n in xml_cell.iter()
                        )
                        if (
                            has_rich_font
                            or alignment is not None
                            and (
                                alignment.get("textRotation", "0") != "0"
                                or alignment.get("indent", "0") != "0"
                            )
                        ):
                            unsupported = True
                            issues.append(
                                issue(
                                    "DISPLAY_TEXT_LAYOUT_UNVERIFIED",
                                    "富文本字体、旋转或缩进超出当前保守显示模型",
                                    location=f"{sheet.name}!{cell.coordinate}",
                                    stage="input",
                                )
                            )
                        name_node = font.find(tag("name"))
                        font_name = (
                            name_node.get("val", "Calibri") if name_node is not None else "Calibri"
                        )
                        if (
                            font_name
                            not in {
                                "Calibri",
                                "Arial",
                                "Aptos",
                                "等线",
                                "宋体",
                                "微软雅黑",
                                "Microsoft YaHei",
                                "SimSun",
                                "DengXian",
                                "Segoe UI",
                                "Tahoma",
                                "Verdana",
                                "Times New Roman",
                                "Courier New",
                            }
                            or not 6 <= font_size <= 72
                        ):
                            unsupported = True
                            issues.append(
                                issue(
                                    "DISPLAY_FONT_UNVERIFIED",
                                    "字体或字号超出保守显示验证范围",
                                    location=f"{sheet.name}!{cell.coordinate}",
                                    actual=font_name,
                                    stage="input",
                                )
                            )
                    row_node = row_nodes.get(cell.row)
                    height = (
                        number(row_node.get("ht", "15")) if row_node is not None else Fraction(15)
                    )
                    capacity = max(Fraction(1), (width - 6) * 11 / max(Fraction(1), font_size))
                    needed_lines = sum(
                        max(1, ceil(Fraction(display_width(line)) / capacity))
                        for line in display.splitlines() or [""]
                    )
                    required_height = (font_size + 9) * needed_lines
                    if (
                        height < required_height
                        or row_node is not None
                        and row_node.get("hidden") == "1"
                    ):
                        if required_height > 409:
                            unsupported = True
                        issues.append(
                            issue(
                                "DISPLAY_ROW_CLIPPED",
                                "行高或隐藏状态不能保证完整可读",
                                location=f"{sheet.name}!{cell.coordinate}",
                                expected=text(required_height),
                                actual=text(height),
                                stage="input" if required_height > 409 else "delivery",
                            )
                        )
                    if numeric and width < max(14, display_width(display) + 4):
                        issues.append(
                            issue(
                                "DISPLAY_COLUMN_TOO_NARROW",
                                "列宽不能证实数值完整可读，可能显示井号或截断",
                                location=f"{sheet.name}!{cell.coordinate}",
                                expected=str(display_width(display) + 4),
                                actual=text(width),
                            )
                        )
                    if re.fullmatch(r"#{4,}", display) or re.fullmatch(r"[+-]?\d+\.", display):
                        issues.append(
                            issue(
                                "DISPLAY_LITERAL_ERROR",
                                "发现井号或整数尾部小数点显示",
                                location=f"{sheet.name}!{cell.coordinate}",
                                actual=display,
                            )
                        )
            book = parse_xml(archive.read("xl/workbook.xml"))
            book_view = book.find(f"{tag('bookViews')}/{tag('workbookView')}")
            active_index = int(book_view.get("activeTab", "0")) if book_view is not None else 0
            names = [s.name for s in workbook.sheets]
            expected_active = (
                manifest.layouts[0].sheet_name
                if prefix == "c" and manifest.layouts
                else next(iter(required), None)
            )
            if expected_active is not None and (
                active_index < 0
                or active_index >= len(names)
                or names[active_index] != expected_active
            ):
                issues.append(
                    issue(
                        "OUTPUT_ACTIVE_SHEET",
                        "工作簿活动页没有定位到首个业务表",
                        expected=expected_active,
                    )
                )
        return (
            VerificationCheck(
                name=f"{prefix}_display",
                status="not_run" if unsupported else "fail" if issues else "pass",
                detail="受限 OOXML 显示边界检查：General/整数/固定时间格式、15 位以内数值、可读列宽、筛选、顶部及冻结；超出能力不视为通过",
            ),
        ), tuple(issues)
