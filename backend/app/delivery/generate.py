"""在完整底稿副本上生成 C 表，附加行、来源和分配严格分开。"""

import json
import posixpath
import re
from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree as ET

from app.delivery.formulas import FormulaUnsupported, recalculate, relative_formula, translate
from app.delivery.models import DeliveryInputs, GenerationManifest, SheetLayout, TraceColumn
from app.delivery.ooxml import (
    COLORS,
    DIRECTION_LABELS,
    METADATA_HEADERS,
    STATUS_LABELS,
    Package,
    column,
    column_number,
    row_number,
)
from app.matching.numbers import number, text
from app.preparation.domain.errors import PreparationError
from app.preparation.domain.models import BusinessRecord, FieldName, FileRole, SheetSnapshot
from app.preparation.domain.values import cell_text
from app.preparation.infrastructure.xlsx import parse_xml, serialize_xml, tag

TRACE_FIELDS: tuple[FieldName, ...] = (
    FieldName.WAREHOUSE,
    FieldName.DATE,
    FieldName.DOCUMENT,
    FieldName.PRODUCT_ID,
    FieldName.PRODUCT_NAME,
    FieldName.SPEC,
    FieldName.UNIT,
    FieldName.IN_QUANTITY,
    FieldName.IN_PRICE,
    FieldName.IN_AMOUNT,
    FieldName.OUT_QUANTITY,
    FieldName.OUT_PRICE,
    FieldName.OUT_AMOUNT,
    FieldName.FIXED_COST,
    FieldName.NOTE,
)


def trace_schema(inputs: DeliveryInputs) -> tuple[TraceColumn, ...]:
    """保留各 B 工作表实际字段表头，同一字段不同表头分别占列。

    Args:
        inputs: 已冻结的来源快照及字段映射。

    Returns:
        稳定顺序的 B 追溯字段定义。
    """
    b_book = next(w for w in inputs.snapshots.workbooks if w.source_file.role == FileRole.B)
    definitions: dict[tuple[FieldName, str], list[str]] = {}
    for mapping in inputs.prepared.task_context.field_mappings:
        if mapping.role != FileRole.B:
            continue
        sheet = next(s for s in b_book.sheets if s.name == mapping.sheet_name)
        for field in TRACE_FIELDS:
            source_col = mapping.fields.get(field)
            if source_col is None:
                continue
            header_cell = sheet.cells.get(f"{source_col}{mapping.header_row}")
            header = (cell_text(header_cell) if header_cell is not None else None) or field.value
            definitions.setdefault((field, header), []).append(sheet.name)
    return tuple(
        TraceColumn(field=field, header=header, source_sheets=tuple(names))
        for (field, header), names in definitions.items()
    )


def actual_trace_field(record: BusinessRecord, field: FieldName) -> bool:
    """限定追溯数值只能写在 B 真实业务方向对应的列。

    Args:
        record: 已按方向拆分的 B 业务记录。
        field: 目标追溯字段。

    Returns:
        当前方向允许显示该来源字段时返回 True。
    """
    if record.direction.value == "in" and field in {
        FieldName.OUT_QUANTITY,
        FieldName.OUT_PRICE,
        FieldName.OUT_AMOUNT,
    }:
        return False
    return not (
        record.direction.value == "out"
        and field in {FieldName.IN_QUANTITY, FieldName.IN_PRICE, FieldName.IN_AMOUNT}
    )


def rewrite_total(
    formula: str,
    coordinate: str,
    source: SheetSnapshot,
    business_rows: set[int],
    row_map: dict[int, int],
) -> str | None:
    """把数量/金额汇总展开为实际业务单元格，排除嵌套小计的重复累计。

    Args:
        formula: 来源汇总公式。
        coordinate: 来源汇总单元格。
        source: 不可变的原底稿快照。
        business_rows: 第一块认定的原始业务行集合。
        row_map: 插行后的原始业务主行位置。

    Returns:
        基于实际业务范围的汇总公式；不属于受支持汇总时返回 None。
    """
    target_column = coordinate.rstrip("0123456789")
    visited: set[str] = set()

    def collect(expression: str) -> set[int] | None:
        """递归识别单列汇总引用，不把未知表达式静默忽略。

        Args:
            expression: 当前层的 SUM 或 SUBTOTAL 表达式。

        Returns:
            需要累计的原始业务行，未知结构返回 None。
        """
        match = re.fullmatch(
            r"(?:SUM|SUBTOTAL)\((.*)\)", expression.replace(" ", ""), re.IGNORECASE
        )
        if match is None:
            return None
        arguments = match.group(1).split(",")
        if expression.upper().startswith("SUBTOTAL"):
            if arguments[0] not in {"9", "109"}:
                return None
            arguments = arguments[1:]
        result: set[int] = set()
        for argument in arguments:
            reference = re.fullmatch(r"\$?([A-Z]+)\$?(\d+)(?::\$?([A-Z]+)\$?(\d+))?", argument)
            if (
                reference is None
                or reference.group(1) != target_column
                or reference.group(3) not in {None, target_column}
            ):
                return None
            start, end = int(reference.group(2)), int(reference.group(4) or reference.group(2))
            for row in range(start, end + 1):
                if row in business_rows:
                    result.add(row)
                else:
                    ref = f"{target_column}{row}"
                    cell = source.cells.get(ref)
                    if cell is not None and cell.kind == "formula":
                        if ref in visited:
                            return None
                        visited.add(ref)
                        nested = collect(cell.formula or "")
                        if nested is None:
                            return None
                        result.update(nested)
                        visited.remove(ref)
                    elif (
                        cell is not None and cell.kind == "number" and cell.value not in {None, "0"}
                    ):
                        return None
        return result

    selected = collect(formula)
    if selected is None or not selected:
        return None
    coordinates = [f"{target_column}{row_map[row]}" for row in sorted(selected)]
    # 连续数值行压缩为范围；文本数值使用 VALUE，遵循 Excel 的 SUM 文本语义。
    arguments: list[str] = []
    run_start: str | None = None
    run_end: str | None = None
    previous_row = -1
    for original_row, output_coordinate in zip(sorted(selected), coordinates, strict=True):
        cell = source.cells.get(f"{target_column}{original_row}")
        is_text = cell is not None and cell.kind == "string"
        output_row = row_map[original_row]
        if is_text or previous_row != output_row - 1:
            if run_start is not None:
                arguments.append(run_start if run_start == run_end else f"{run_start}:{run_end}")
            run_start = None
            if is_text:
                arguments.append(f"VALUE({output_coordinate})")
            else:
                run_start = run_end = output_coordinate
        else:
            run_end = output_coordinate
        previous_row = output_row if not is_text else -1
    if run_start is not None:
        arguments.append(run_start if run_start == run_end else f"{run_start}:{run_end}")
    return "SUM(" + ",".join(arguments) + ")"


class CGenerator:
    """完整复制底稿，在来源业务主行及其下方展开真实 B 追溯。"""

    def generate(
        self, baseline_path: Path, inputs: DeliveryInputs, generation_version: int
    ) -> tuple[bytes, GenerationManifest]:
        """生成 C 文件和可审计的布局描述。

        Args:
            baseline_path: 第一块完整复制的 C 初始底稿。
            inputs: 固定来源、正式匹配组和确认信息。
            generation_version: 当前生成版本。

        Returns:
            完整 C 工作簿字节及布局、审计表和公式能力说明。
        """
        package = Package(baseline_path)
        schema = trace_schema(inputs)
        b_records = {r.record_id: r for r in inputs.b_records}
        row_results = {r.a_record_id: r for r in inputs.matches.rows}
        by_a = {
            identifier: g for g in inputs.matches.groups for identifier in g.candidate.a_record_ids
        }
        owners: dict[str, str] = {}
        for record in inputs.a_records:
            if record.record_id in by_a:
                owners.setdefault(by_a[record.record_id].group_id, record.record_id)
        traces: dict[str, tuple[str, ...]] = {
            r.record_id: by_a[r.record_id].candidate.b_record_ids
            if r.record_id in by_a and owners[by_a[r.record_id].group_id] == r.record_id
            else ()
            for r in inputs.a_records
        }
        layouts: list[SheetLayout] = []
        maps: dict[str, dict[int, int]] = {}
        extras: dict[str, dict[int, int]] = {}
        for mapping in inputs.prepared.task_context.field_mappings:
            if mapping.role != FileRole.A:
                continue
            source = next(s for s in inputs.baseline.sheets if s.name == mapping.sheet_name)
            root = package.sheet(source.name)
            records = tuple(r for r in inputs.a_records if r.source.sheet_name == source.name)
            original_max = max(
                source.max_row,
                *(
                    int(row.get("r", "0"))
                    for row in root.findall(f"{tag('sheetData')}/{tag('row')}")
                ),
            )
            extra = {r.source.row: max(0, len(traces[r.record_id]) - 1) for r in records}
            if original_max + sum(extra.values()) > 1048576:
                raise PreparationError(
                    "OUTPUT_ROW_LIMIT", "展开后的 C 超过 Excel 行数上限，不能生成截断文件"
                )
            row_map: dict[int, int] = {}
            shift = 0
            for original_index in range(1, original_max + 1):
                row_map[original_index] = original_index + shift
                shift += extra.get(original_index, 0)
            maps[source.name], extras[source.name] = row_map, extra
            original_cols = max((column_number(c.column) for c in source.cells.values()), default=1)
            metadata = {
                name: column(original_cols + i + 1) for i, name in enumerate(METADATA_HEADERS)
            }
            column(original_cols + len(METADATA_HEADERS) + len(schema))
            layouts.append(
                SheetLayout(
                    sheet_name=source.name,
                    header_row=mapping.header_row,
                    original_columns=original_cols,
                    original_max_row=original_max,
                    row_map={str(k): v for k, v in row_map.items()},
                    metadata=metadata,
                    trace_columns=schema,
                )
            )
            self._expand_shared(root)
            data = root.find(tag("sheetData"))
            if data is None:
                raise ValueError("底稿工作表缺少数据")
            expanded_rows: list[ET.Element] = []
            for row in list(data):
                original_row = int(row.get("r", "0"))
                output_row = row_map[original_row]
                row.set("r", str(output_row))
                for cell in row:
                    coordinate = cell.get("r", "A1")
                    cell.set("r", coordinate.rstrip("0123456789") + str(output_row))
                expanded_rows.append(row)
                for offset in range(extra.get(original_row, 0)):
                    added = ET.Element(tag("row"), {"r": str(output_row + offset + 1)})
                    expanded_rows.append(added)
            data[:] = expanded_rows
            for record in records:
                output_row = row_map[record.source.row]
                result = row_results[record.record_id]
                group = by_a.get(record.record_id)
                allocated_q = (
                    sum(
                        (
                            number(x.quantity)
                            for x in group.candidate.allocations
                            if x.a_record_id == record.record_id
                        ),
                        Fraction(),
                    )
                    if group
                    else Fraction()
                )
                allocated_m = (
                    sum(
                        (
                            number(x.a_amount)
                            for x in group.candidate.allocations
                            if x.a_record_id == record.record_id
                        ),
                        Fraction(),
                    )
                    if group
                    else Fraction()
                )
                reason = "；".join(result.reasons) or (
                    group.candidate.rule_id if group else "规则范围内没有确定关系"
                )
                for offset in range(max(1, len(traces[record.record_id]))):
                    current_row = output_row + offset
                    color = COLORS[result.status]
                    for col in range(1, original_cols + 1):
                        cell = package.cell(root, f"{column(col)}{current_row}")
                        cell.set(
                            "s",
                            str(
                                package.style(
                                    int(cell.get("s", "0")),
                                    color=color,
                                    number_format="General",
                                )
                            ),
                        )
                    values: dict[str, str | None] = {
                        "匹配状态": STATUS_LABELS[result.status],
                        "匹配依据": reason,
                        "匹配组": group.group_id if group else None,
                    }
                    if offset == 0:
                        values.update(
                            {
                                "A来源标识": record.record_id,
                                "A已对应数量": text(allocated_q),
                                "A已对应金额": text(allocated_m),
                                "A未结数量": result.remaining_quantity,
                                "A未结金额": result.remaining_amount,
                            }
                        )
                    if traces[record.record_id]:
                        b = b_records[traces[record.record_id][offset]]
                        if group is None:
                            raise ValueError("追溯关系缺少正式匹配组")
                        bq = sum(
                            (
                                number(x.quantity)
                                for x in group.candidate.allocations
                                if x.b_record_id == b.record_id
                            ),
                            Fraction(),
                        )
                        bm = sum(
                            (
                                number(x.b_amount)
                                for x in group.candidate.allocations
                                if x.b_record_id == b.record_id
                            ),
                            Fraction(),
                        )
                        values.update(
                            {
                                "B来源标识": b.record_id,
                                "B原始工作表": b.source.sheet_name,
                                "B原始行号": str(b.source.row),
                                "匹配方向": DIRECTION_LABELS[b.direction.value],
                                "B已对应数量": text(bq),
                                "B已对应金额": text(bm),
                                "B未结数量": text(abs(number(b.normalized_fields.quantity)) - bq),
                                "B未结金额": text(abs(number(b.normalized_fields.amount)) - bm),
                            }
                        )
                        for i, definition in enumerate(schema):
                            source_cell = (
                                b.raw_fields.get(definition.field)
                                if b.source.sheet_name in definition.source_sheets
                                and actual_trace_field(b, definition.field)
                                else None
                            )
                            package.copy_value(
                                root,
                                f"{column(original_cols + len(METADATA_HEADERS) + i + 1)}{current_row}",
                                source_cell,
                                color=color,
                            )
                    for name, value in values.items():
                        package.write(
                            root,
                            f"{metadata[name]}{current_row}",
                            value,
                            numeric=name
                            in {
                                "B原始行号",
                                "A已对应数量",
                                "A已对应金额",
                                "A未结数量",
                                "A未结金额",
                                "B已对应数量",
                                "B已对应金额",
                                "B未结数量",
                                "B未结金额",
                            },
                            color=color,
                        )
            for name, metadata_col in metadata.items():
                package.write(
                    root, f"{metadata_col}{mapping.header_row}", name, color=COLORS["header"]
                )
            for i, definition in enumerate(schema):
                package.write(
                    root,
                    f"{column(original_cols + len(METADATA_HEADERS) + i + 1)}{mapping.header_row}",
                    definition.header,
                    color=COLORS["header"],
                )
        self._translate_package(package, inputs, maps, extras)
        audit_name, allocation_name, review_name = self._audit(package, inputs, layouts)
        problems = recalculate(package)
        for layout in layouts:
            root = package.sheet(layout.sheet_name)
            last_row = layout.original_max_row + sum(extras[layout.sheet_name].values())
            package.finish_sheet(
                root,
                layout.header_row,
                layout.original_columns + len(METADATA_HEADERS) + len(schema),
                last_row,
            )
        for name in (audit_name, allocation_name, review_name):
            root = package.sheet(name)
            cells = list(root.iter(tag("c")))
            package.finish_sheet(
                root,
                1,
                max(
                    (column_number(c.get("r", "A1").rstrip("0123456789")) for c in cells), default=1
                ),
                max((row_number(c.get("r", "A1")) for c in cells), default=1),
            )
        manifest = GenerationManifest(
            task_id=inputs.plan.task_id,
            data_version=inputs.plan.prepared_data_version,
            match_plan_version=inputs.plan.match_plan_version,
            generation_version=generation_version,
            layouts=tuple(layouts),
            audit_sheet=audit_name,
            allocation_sheet=allocation_name,
            review_sheet=review_name,
            formula_problems=problems,
        )
        if layouts:
            package.activate(layouts[0].sheet_name)
        return package.save(), manifest

    def _expand_shared(self, root: ET.Element) -> None:
        """在原坐标下展开共享公式，避免插行后共享偏移失效。

        Args:
            root: 尚未插行的原工作表。
        """
        anchors = {
            f.get("si", ""): (c.get("r", "A1"), f.text or "")
            for c in root.iter(tag("c"))
            if (f := c.find(tag("f"))) is not None and f.get("t") == "shared" and f.text
        }
        for cell in root.iter(tag("c")):
            formula = cell.find(tag("f"))
            if formula is None or formula.get("t") != "shared":
                continue
            anchor, expression = anchors.get(formula.get("si", ""), ("A1", ""))
            current = cell.get("r", "A1")
            formula.text = relative_formula(
                expression,
                row_number(current) - row_number(anchor),
                column_number(current.rstrip("0123456789"))
                - column_number(anchor.rstrip("0123456789")),
            )
            formula.attrib.clear()

    def _translate_package(
        self,
        package: Package,
        inputs: DeliveryInputs,
        maps: dict[str, dict[int, int]],
        extras: dict[str, dict[int, int]],
    ) -> None:
        """更新公式、定义名称和相关部件范围，源资产仍随原包保留。

        Args:
            package: 已完成插行的工作簿包。
            inputs: 原始公式及业务范围证据。
            maps: 全部 A 工作表的行映射。
            extras: 各原始业务行后增加的行数。
        """
        for name in package.sheet_paths():
            root = package.sheet(name)
            source = next((s for s in inputs.baseline.sheets if s.name == name), None)
            records = tuple(r for r in inputs.a_records if r.source.sheet_name == name)
            numeric_columns = {
                r.source.cells[f].rstrip("0123456789")
                for r in records
                for f in (FieldName.QUANTITY, FieldName.AMOUNT)
                if f in r.source.cells
            }
            reverse = {v: k for k, v in maps.get(name, {}).items()}
            for cell in root.iter(tag("c")):
                formula = cell.find(tag("f"))
                if formula is None:
                    continue
                original = formula.text or ""
                coordinate = cell.get("r", "A1")
                original_row = reverse.get(row_number(coordinate))
                replacement = None
                if (
                    source is not None
                    and original_row is not None
                    and coordinate.rstrip("0123456789") in numeric_columns
                ):
                    replacement = rewrite_total(
                        original,
                        coordinate.rstrip("0123456789") + str(original_row),
                        source,
                        {r.source.row for r in records},
                        maps[name],
                    )
                try:
                    formula.text = replacement or translate(original, name, maps, extras)
                except FormulaUnsupported:
                    # 保留无法解释的公式，重算/验收会明确阻止正式交付。
                    formula.text = original
            if name in maps:
                for element in root.iter():
                    if element.tag in {tag("f"), tag("c"), tag("row")}:
                        continue
                    for attribute in ("ref", "sqref"):
                        value = element.get(attribute)
                        if value:
                            try:
                                element.set(attribute, translate(value, name, maps, extras))
                            except FormulaUnsupported:
                                pass
                sheet_path = package.sheet_paths()[name]
                rel_path = posixpath.join(
                    posixpath.dirname(sheet_path), "_rels", posixpath.basename(sheet_path) + ".rels"
                )
                if rel_path in package.parts:
                    for relation in parse_xml(package.parts[rel_path]):
                        if relation.get("Type", "").endswith("/table"):
                            target = posixpath.normpath(
                                posixpath.join(
                                    posixpath.dirname(sheet_path), relation.get("Target", "")
                                )
                            ).lstrip("/")
                            original_xml = package.parts[target]
                            table = parse_xml(original_xml)
                            for node in table.iter():
                                if node.get("ref"):
                                    node.set(
                                        "ref", translate(node.get("ref", ""), name, maps, extras)
                                    )
                            for style in list(table.findall(tag("tableStyleInfo"))):
                                table.remove(style)
                            package.parts[target] = serialize_xml(table, original_xml)
        for defined_name in package.workbook.findall(f"{tag('definedNames')}/{tag('definedName')}"):
            try:
                defined_name.text = translate(defined_name.text or "", "", maps, extras)
            except FormulaUnsupported:
                pass
        for path, part_original in tuple(package.parts.items()):
            if not path.startswith("xl/charts/") or not path.endswith(".xml"):
                continue
            root = parse_xml(part_original)
            for formula in root.iter():
                if formula.tag.endswith("}f") and formula.text:
                    try:
                        formula.text = translate(formula.text, "", maps, extras)
                    except FormulaUnsupported:
                        pass
            package.parts[path] = serialize_xml(root, part_original)

    def _audit(
        self, package: Package, inputs: DeliveryInputs, layouts: list[SheetLayout]
    ) -> tuple[str, str, str]:
        """保存稳定来源、原公式、正式分配及独立候选复核明细。

        Args:
            package: C 输出包。
            inputs: 不可变来源及正式匹配信息。
            layouts: 已计算的各业务表原行映射。

        Returns:
            来源审计表、分配表和待复核表的实际名称。
        """
        audit_name, audit = package.add_sheet("_对账来源")
        allocation_name, allocation = package.add_sheet("_对账分配")
        review_name, review = package.add_sheet("_待复核")
        headers = (
            "角色",
            "来源标识",
            "原工作表",
            "原始行号",
            "C工作表",
            "C行号",
            "方向",
            "原字段及公式",
        )
        for i, header in enumerate(headers):
            package.write(audit, f"{column(i + 1)}1", header, color=COLORS["header"])
        positions = {
            r.record_id: (
                r.source.sheet_name,
                next(l for l in layouts if l.sheet_name == r.source.sheet_name).row_map[
                    str(r.source.row)
                ],
            )
            for r in inputs.a_records
        }
        for row, record in enumerate((*inputs.a_records, *inputs.b_records), 2):
            c_sheet, c_row = positions.get(record.record_id, ("", 0))
            fields = json.dumps(
                {
                    f.value: {
                        "cell": c.coordinate,
                        "value": c.value,
                        "formula": c.formula,
                        "cached": c.cached_value,
                    }
                    for f, c in record.raw_fields.items()
                },
                ensure_ascii=False,
            )
            values = (
                "A" if record.record_id in positions else "B",
                record.record_id,
                record.source.sheet_name,
                str(record.source.row),
                c_sheet,
                str(c_row) if c_row else "",
                DIRECTION_LABELS[record.direction.value],
                fields,
            )
            for i, value in enumerate(values):
                package.write(audit, f"{column(i + 1)}{row}", value)
        for i, header in enumerate(
            ("匹配组", "A来源标识", "B来源标识", "分配数量", "A分配金额", "B分配金额", "A对应C位置")
        ):
            package.write(allocation, f"{column(i + 1)}1", header, color=COLORS["header"])
        row = 2
        for group in inputs.matches.groups:
            for edge in group.candidate.allocations:
                sheet, c_row = positions[edge.a_record_id]
                for i, value in enumerate(
                    (
                        group.group_id,
                        edge.a_record_id,
                        edge.b_record_id,
                        edge.quantity,
                        edge.a_amount,
                        edge.b_amount,
                        f"{sheet}!{c_row}",
                    )
                ):
                    package.write(
                        allocation, f"{column(i + 1)}{row}", value, numeric=i in {3, 4, 5}
                    )
                row += 1
        for i, header in enumerate(
            ("事项", "A来源标识", "候选B标识（非正式承接）", "候选标识", "复核原因")
        ):
            package.write(review, f"{column(i + 1)}1", header, color=COLORS["header"])
        for row, item in enumerate(inputs.matches.reviews, 2):
            for i, value in enumerate(
                (
                    item.code,
                    " | ".join(item.a_record_ids),
                    " | ".join(item.b_record_ids),
                    " | ".join(item.candidate_ids),
                    "；".join(item.reasons),
                )
            ):
                package.write(review, f"{column(i + 1)}{row}", value)
        return audit_name, allocation_name, review_name
