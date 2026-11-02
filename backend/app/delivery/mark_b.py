"""从实际 C 文件的核查事实生成 B 副本标注，源行和源值保持原位置。"""

from collections import defaultdict
from pathlib import Path

from app.delivery.formulas import recalculate
from app.delivery.models import BCoverage, DeliveryInputs
from app.delivery.ooxml import B_HEADERS, COLORS, DIRECTION_LABELS, Package, column, column_number
from app.preparation.domain.models import FileRole
from app.preparation.infrastructure.xlsx import tag


def annotation_values(records: tuple[BCoverage, ...]) -> tuple[str, ...]:
    """把同一 B 原始行的各方向事实分别表达在标注中。

    Args:
        records: 同一来源行的全部入库、出库或未知方向记录。

    Returns:
        对应固定标注表头的字段值；双向行不会把两个方向混为一类。
    """
    labels = {
        "taken": "已承接",
        "missing": "C表缺失",
        "excluded": "人工排除",
        "outside_period": "非本月",
        "unknown_period": "月份待确认",
    }

    def combined(values: tuple[str, ...]) -> str:
        """为单向或双向来源行组合明确文字。

        Args:
            values: 与各方向记录一一对应的字段文字。

        Returns:
            单向值或带独立方向前缀的多向值。
        """
        if len(records) == 1:
            return values[0]
        return " | ".join(
            f"{DIRECTION_LABELS[r.direction]}：{value}"
            for r, value in zip(records, values, strict=True)
        )

    return (
        combined(tuple(labels[r.disposition] for r in records)),
        combined(tuple(r.missing_type for r in records)),
        " | ".join(DIRECTION_LABELS[r.direction] for r in records),
        combined(
            tuple(
                r.remaining_quantity if r.remaining_quantity is not None else "不可计算"
                for r in records
            )
        ),
        combined(
            tuple(
                r.remaining_amount if r.remaining_amount is not None else "不可计算"
                for r in records
            )
        ),
        combined(tuple(r.explanation for r in records)),
        "基于实际C单元格重新核查；各方向独立计数，原始行只保留一次",
        " | ".join(dict.fromkeys(i for r in records for i in r.a_record_ids)),
        " | ".join(dict.fromkeys(i for r in records for i in r.c_references)),
        " | ".join(dict.fromkeys(i for r in records for i in r.group_ids)),
        "",  # 没有明确的人工参考表对应行时不编造“人工表有”。
        combined(tuple(r.exclusion_reason or "" for r in records)),
        combined(tuple(r.confirmation_ref or "" for r in records)),
        " | ".join(r.b_record_id for r in records),
    )


def mark_b(path: Path, inputs: DeliveryInputs, coverage: tuple[BCoverage, ...]) -> bytes:
    """在原始 B 副本右侧追加实际 C 反向核查结果。

    Args:
        path: 冻结的原始 B 工作簿。
        inputs: 原始工作簿结构、映射及确认信息。
        coverage: 依据真实 C 得到的全部方向去向。

    Returns:
        原行未插删、源值未改写的标注 B 工作簿字节。
    """
    package = Package(path)
    by_source_row: dict[tuple[str, int], list[BCoverage]] = defaultdict(list)
    for record in coverage:
        by_source_row[(record.source_sheet, record.source_row)].append(record)
    source = next(w for w in inputs.snapshots.workbooks if w.source_file.role == FileRole.B)
    for mapping in inputs.prepared.task_context.field_mappings:
        if mapping.role != FileRole.B:
            continue
        sheet = next(s for s in source.sheets if s.name == mapping.sheet_name)
        root = package.sheet(sheet.name)
        original_cols = max((column_number(c.column) for c in sheet.cells.values()), default=1)
        for i, header in enumerate(B_HEADERS):
            package.write(
                root,
                f"{column(original_cols + i + 1)}{mapping.header_row}",
                header,
                color=COLORS["header"],
            )
        rows = {r.source_row for r in coverage if r.source_sheet == sheet.name}
        for row in rows:
            records = tuple(by_source_row[(sheet.name, row)])
            color = (
                COLORS["unresolved"]
                if any(r.disposition in {"missing", "unknown_period"} for r in records)
                else ""
            )
            for cell in root.findall(f"{tag('sheetData')}/{tag('row')}[@r='{row}']/{tag('c')}"):
                cell.set(
                    "s",
                    str(
                        package.style(
                            int(cell.get("s", "0")),
                            color=color,
                            number_format="yyyy-mm-dd hh:mm:ss"
                            if cell.get("r", "").rstrip("0123456789")
                            == mapping.fields.get(
                                next(f for f in mapping.fields if f.value == "date")
                            )
                            and cell.get("t", "n") == "n"
                            else "General",
                        )
                    ),
                )
            for i, value in enumerate(annotation_values(records)):
                package.write(root, f"{column(original_cols + i + 1)}{row}", value, color=color)
        package.finish_sheet(
            root, mapping.header_row, original_cols + len(B_HEADERS), sheet.max_row
        )
    recalculate(package)
    first_mapping = next(
        m for m in inputs.prepared.task_context.field_mappings if m.role == FileRole.B
    )
    package.activate(first_mapping.sheet_name)
    return package.save()
