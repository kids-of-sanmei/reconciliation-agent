"""直接读写 OOXML，完整复制未加工部件并保留数值文本和工作簿资产。"""

import hashlib
import io
import posixpath
import re
from copy import deepcopy
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from app.preparation.domain.errors import PreparationError
from app.preparation.domain.models import (
    AuditEvent,
    AuditTrail,
    BusinessRecord,
    CellSnapshot,
    FieldName,
    SheetMapping,
    SheetSnapshot,
    SourceFile,
    WorkbookSnapshot,
)
from app.preparation.domain.values import cell_text

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
ET.register_namespace("", NS)
ET.register_namespace("r", REL_NS)
BUILTIN_FORMATS: dict[int, str] = {
    0: "General",
    1: "0",
    2: "0.00",
    14: "mm-dd-yy",
    15: "d-mmm-yy",
    16: "d-mmm",
    17: "mmm-yy",
    18: "h:mm AM/PM",
    19: "h:mm:ss AM/PM",
    20: "h:mm",
    21: "h:mm:ss",
    22: "m/d/yy h:mm",
    45: "mm:ss",
    46: "[h]:mm:ss",
}

type LiteralCellKind = Literal["blank", "string", "number", "boolean", "date", "formula", "error"]


def tag(name: str) -> str:
    """返回带 OOXML 命名空间的标签。

    Args:
        name: 不带前缀的元素名称。

    Returns:
        ElementTree 使用的完整标签。
    """
    return f"{{{NS}}}{name}"


def parse_xml(content: bytes) -> ET.Element:
    """解析不允许文档类型声明的工作簿 XML。

    Args:
        content: ZIP 部件中的 XML 原始字节。

    Returns:
        XML 根节点。
    """
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise PreparationError("INVALID_XML", "工作簿 XML 不支持文档类型或实体声明")
    try:
        return ET.fromstring(content)
    except ET.ParseError as exc:
        raise PreparationError("INVALID_XML", "工作簿包含无法解析的 XML") from exc


def serialize_xml(root: ET.Element, original: bytes) -> bytes:
    """序列化 XML 并保留兼容性属性引用的命名空间声明。

    Args:
        root: 加工后的根节点。
        original: 原 XML，用于恢复 mc:Ignorable 等引用但未实际用作标签的前缀。

    Returns:
        保留声明的 UTF-8 XML。
    """
    result = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    declarations = re.findall(rb'xmlns:([A-Za-z_][\w.-]*)=["\']([^"\']+)["\']', original)
    for prefix, uri in declarations:
        if b"xmlns:" + prefix + b"=" not in result:
            root.set("xmlns:" + prefix.decode("ascii"), uri.decode("utf-8"))
    return ET.tostring(root, encoding="unicode", xml_declaration=True).encode("utf-8")


class XlsxGateway:
    """原始 ZIP 部件保持完整，仅修改业务工作表及其必要样式。"""

    def __init__(self, *, max_archive_bytes: int, max_cells: int) -> None:
        """设置解析资源边界。

        Args:
            max_archive_bytes: 工作簿所有部件解压后的最大总字节数。
            max_cells: 单个工作簿允许读取的最大显式单元格数。
        """
        self.max_archive_bytes = max_archive_bytes
        self.max_cells = max_cells

    def _archive(self, path: Path) -> dict[str, bytes]:
        """读取工作簿部件。

        Args:
            path: 保存后的 xlsx 文件。

        Returns:
            ZIP 内路径到原始部件字节的映射。
        """
        try:
            with ZipFile(path) as archive:
                members = archive.infolist()
                if sum(info.file_size for info in members) > self.max_archive_bytes:
                    raise PreparationError("WORKBOOK_TOO_LARGE", "工作簿解压后超出处理上限")
                if len({info.filename for info in members}) != len(members):
                    raise PreparationError("INVALID_WORKBOOK", "工作簿包含重复部件")
                return {info.filename: archive.read(info) for info in members}
        except (BadZipFile, RuntimeError, OSError) as exc:
            raise PreparationError("INVALID_WORKBOOK", "文件损坏、加密或不是有效的 xlsx") from exc

    def read(self, path: Path, source: SourceFile) -> WorkbookSnapshot:
        """读取工作簿及完整单元格来源。

        Args:
            path: 保存后的文件路径。
            source: 输入文件的角色、身份和摘要。

        Returns:
            不经过浮点转换的工作簿快照。
        """
        parts = self._archive(path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != source.sha256:
            raise PreparationError("SOURCE_CHANGED", "输入原文件内容已变化")
        try:
            workbook = parse_xml(parts["xl/workbook.xml"])
            relationships = parse_xml(parts["xl/_rels/workbook.xml.rels"])
        except KeyError as exc:
            raise PreparationError("INVALID_WORKBOOK", "工作簿缺少必要结构") from exc
        targets: dict[str, str] = {}
        for relationship in relationships:
            target = relationship.get("Target", "")
            if relationship.get("TargetMode") != "External":
                resolved = posixpath.normpath(posixpath.join("xl", target)).lstrip("/")
                targets[relationship.get("Id", "")] = resolved
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in parts:
            for item in parse_xml(parts["xl/sharedStrings.xml"]):
                shared_strings.append("".join(node.text or "" for node in item.iter(tag("t"))))
        formats, styles = self._styles(parts)
        sheets: list[SheetSnapshot] = []
        total_cells = 0
        for sheet in workbook.findall(f"{tag('sheets')}/{tag('sheet')}"):
            xml_path = targets.get(sheet.get(f"{{{REL_NS}}}id", ""), "")
            if xml_path not in parts:
                raise PreparationError("INVALID_WORKBOOK", "工作表部件不存在")
            root = parse_xml(parts[xml_path])
            cells: dict[str, CellSnapshot] = {}
            for element in root.iter(tag("c")):
                total_cells += 1
                if total_cells > self.max_cells:
                    raise PreparationError("TOO_MANY_CELLS", "工作簿单元格数超出处理上限")
                cell = self._cell(element, shared_strings, formats, styles)
                if cell.coordinate in cells:
                    raise PreparationError("DUPLICATE_CELL", "工作表存在重复单元格坐标")
                cells[cell.coordinate] = cell
            rows = root.findall(f"{tag('sheetData')}/{tag('row')}")
            columns = root.findall(f"{tag('cols')}/{tag('col')}")
            sheets.append(
                SheetSnapshot(
                    sheet_id=sheet.get("sheetId", ""),
                    name=sheet.get("name", ""),
                    xml_path=xml_path,
                    max_row=max((cell.row for cell in cells.values()), default=0),
                    cells=cells,
                    merged_ranges=tuple(
                        node.get("ref", "")
                        for node in root.findall(f"{tag('mergeCells')}/{tag('mergeCell')}")
                    ),
                    hidden_rows=tuple(
                        int(row.get("r", "0")) for row in rows if row.get("hidden") == "1"
                    ),
                    hidden_columns=tuple(
                        f"{column.get('min')}:{column.get('max')}"
                        for column in columns
                        if column.get("hidden") == "1"
                    ),
                    row_dimensions=tuple(
                        ET.tostring(row, encoding="unicode").split(">")[0] for row in rows
                    ),
                    column_dimensions=tuple(
                        ET.tostring(column, encoding="unicode") for column in columns
                    ),
                )
            )
        if not sheets:
            raise PreparationError("EMPTY_WORKBOOK", "工作簿没有可读取的工作表")
        properties = workbook.find(tag("workbookPr"))
        epoch: Literal["1900", "1904"] = (
            "1904"
            if properties is not None and properties.get("date1904") in {"1", "true"}
            else "1900"
        )
        return WorkbookSnapshot(source_file=source, date_system=epoch, sheets=tuple(sheets))

    def _styles(self, parts: dict[str, bytes]) -> tuple[dict[int, str], list[int]]:
        """解析数字格式及样式索引。

        Args:
            parts: 工作簿 ZIP 部件。

        Returns:
            格式编号映射及每个单元格样式的数字格式编号。
        """
        formats = dict(BUILTIN_FORMATS)
        styles = [0]
        if "xl/styles.xml" in parts:
            root = parse_xml(parts["xl/styles.xml"])
            for element in root.findall(f"{tag('numFmts')}/{tag('numFmt')}"):
                formats[int(element.get("numFmtId", "0"))] = element.get("formatCode", "General")
            xfs = root.find(tag("cellXfs"))
            if xfs is not None:
                styles = [int(xf.get("numFmtId", "0")) for xf in xfs]
        return formats, styles

    def _cell(
        self, element: ET.Element, strings: list[str], formats: dict[int, str], styles: list[int]
    ) -> CellSnapshot:
        """解析一个显式单元格。

        Args:
            element: 单元格 XML。
            strings: 共享字符串表。
            formats: 数字格式定义。
            styles: 样式到数字格式编号的映射。

        Returns:
            保留公式、缓存和格式的单元格快照。
        """
        coordinate = element.get("r", "")
        match = re.fullmatch(r"([A-Z]{1,3})([1-9]\d*)", coordinate)
        if match is None:
            raise PreparationError("INVALID_CELL", "单元格缺少有效坐标")
        style_id = int(element.get("s", "0"))
        if style_id >= len(styles) or style_id < 0:
            raise PreparationError("INVALID_STYLE", "单元格样式索引无效")
        value_node = element.find(tag("v"))
        raw = value_node.text if value_node is not None else None
        value_type = element.get("t", "n")
        value = raw
        if value_type == "s" and raw is not None:
            try:
                value = strings[int(raw)]
            except (ValueError, IndexError) as exc:
                raise PreparationError("INVALID_STRING", "共享字符串索引无效") from exc
        elif value_type == "inlineStr":
            value = "".join(node.text or "" for node in element.iter(tag("t")))
        formula = element.find(tag("f"))
        kind: LiteralCellKind = "blank"
        if formula is not None:
            kind = "formula"
        elif value is not None:
            cell_types: dict[str, LiteralCellKind] = {
                "s": "string",
                "str": "string",
                "inlineStr": "string",
                "b": "boolean",
                "e": "error",
                "d": "date",
            }
            kind = cell_types.get(value_type, "number")
        return CellSnapshot(
            coordinate=coordinate,
            row=int(match.group(2)),
            column=match.group(1),
            kind=kind,
            value=None if formula is not None else value,
            formula=formula.text or "" if formula is not None else None,
            shared_formula_index=(
                formula.get("si") if formula is not None and formula.get("t") == "shared" else None
            ),
            cached_value=value if formula is not None else None,
            cached_type=value_type if formula is not None else None,
            style_id=style_id,
            number_format=formats.get(styles[style_id], "General"),
        )

    def create_baseline(
        self,
        path: Path,
        snapshot: WorkbookSnapshot,
        mappings: tuple[SheetMapping, ...],
        records: tuple[BusinessRecord, ...],
        period: str,
        formula_values: dict[str, str],
    ) -> tuple[bytes, AuditTrail]:
        """在原工作簿副本上解除合并、补日期、清底色，并承接公式结果。

        Args:
            path: 不可变 A 来源文件。
            snapshot: 来源结构快照。
            mappings: 选定的 A 工作表映射。
            records: 已成功规范化的 A 明细。
            period: 已确认年月。
            formula_values: 已留审计的公式补充值。

        Returns:
            新工作簿字节及操作审计，未加工的 ZIP 部件内容保持不变。
        """
        parts = self._archive(path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != snapshot.source_file.sha256:
            raise PreparationError("SOURCE_CHANGED", "生成底稿前发现 A 原文件已变化")
        styles_original = parts.get("xl/styles.xml")
        styles = parse_xml(styles_original) if styles_original is not None else None
        style_map: dict[str, str] = {}
        events: list[AuditEvent] = []
        for mapping in mappings:
            sheet = next(item for item in snapshot.sheets if item.name == mapping.sheet_name)
            original = parts[sheet.xml_path]
            root = parse_xml(original)
            sheet_records = tuple(item for item in records if item.source.sheet_name == sheet.name)
            numeric_coordinates = {
                record.source.cells[field]
                for record in sheet_records
                for field in (
                    FieldName.QUANTITY,
                    FieldName.UNIT_PRICE,
                    FieldName.AMOUNT,
                    FieldName.FIXED_COST,
                )
                if field in record.source.cells
            }
            by_coordinate = {element.get("r", ""): element for element in root.iter(tag("c"))}
            for record in sheet_records:
                coordinate = record.source.cells[FieldName.DATE]
                cell = by_coordinate.get(coordinate)
                if cell is None:
                    cell = self._insert_cell(root, coordinate, record.source.row)
                    by_coordinate[coordinate] = cell
                raw_cell = record.raw_fields.get(FieldName.DATE)
                if raw_cell is not None:
                    cell.set("s", str(raw_cell.style_id))
                normalized_date = record.normalized_fields.date
                if normalized_date is not None:
                    month, day = normalized_date[5:7], normalized_date[8:10]
                    display = f"{int(month)}月{int(day)}日"
                    self._set_value(cell, display, text=True)
                    events.append(
                        AuditEvent(
                            action="fill_date",
                            source_ref=record.record_id,
                            before=cell_text(raw_cell) if raw_cell else None,
                            after=display,
                        )
                    )
                for field in (FieldName.QUANTITY, FieldName.UNIT_PRICE, FieldName.AMOUNT):
                    source_cell = record.raw_fields.get(field)
                    if source_cell is not None and source_cell.kind == "formula":
                        formula_ref = f"{snapshot.source_file.file_id}:{sheet.sheet_id}:{source_cell.coordinate}"
                        value = formula_values.get(formula_ref, source_cell.cached_value)
                        if value is None:
                            raise PreparationError("MISSING_FORMULA_VALUE", "公式缺少已核实的结果")
                        self._set_value(by_coordinate[source_cell.coordinate], value, text=False)
                        events.append(
                            AuditEvent(
                                action="materialize_formula",
                                source_ref=formula_ref,
                                before=source_cell.formula,
                                after=value,
                            )
                        )
            merges = root.find(tag("mergeCells"))
            if merges is not None:
                root.remove(merges)
                for merged in sheet.merged_ranges:
                    events.append(
                        AuditEvent(
                            action="unmerge",
                            source_ref=f"{sheet.name}!{merged}",
                            before=merged,
                            after=None,
                        )
                    )
            for coordinate, element in by_coordinate.items():
                source_cell = sheet.cells.get(coordinate)
                if source_cell is not None and source_cell.kind == "string":
                    text = cell_text(source_cell) or ""
                    if source_cell.row <= mapping.header_row and re.fullmatch(
                        r"(?:[一二三四五六七八九十]+|\d{1,2})月", text.strip()
                    ):
                        display = self._month_title(int(period[5:7]))
                        self._set_value(element, display, text=True)
                        events.append(
                            AuditEvent(
                                action="month_title",
                                source_ref=f"{sheet.name}!{coordinate}",
                                before=text,
                                after=display,
                            )
                        )
                if styles is not None:
                    self._clear_fill(
                        element, styles, style_map, numeric_format=coordinate in numeric_coordinates
                    )
            parts[sheet.xml_path] = serialize_xml(root, original)
            events.append(
                AuditEvent(action="clear_fill", source_ref=sheet.name, before=None, after=None)
            )
        if styles is not None and styles_original is not None:
            parts["xl/styles.xml"] = serialize_xml(styles, styles_original)
        if any(event.action == "materialize_formula" for event in events):
            self._invalidate_calculation_chain(parts)
        buffer = io.BytesIO()
        with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
            for name, content in parts.items():
                archive.writestr(name, content)
        return buffer.getvalue(), AuditTrail(events=tuple(events))

    @staticmethod
    def _invalidate_calculation_chain(parts: dict[str, bytes]) -> None:
        """公式物化后移除过期计算链，并要求 Excel 打开底稿时重新计算其余公式。

        Args:
            parts: 即将输出的工作簿部件，原始 A 文件不会被修改。
        """
        workbook_original = parts["xl/workbook.xml"]
        workbook = parse_xml(workbook_original)
        calculation = workbook.find(tag("calcPr"))
        if calculation is None:
            calculation = ET.SubElement(workbook, tag("calcPr"))
        calculation.set("fullCalcOnLoad", "1")
        calculation.set("forceFullCalc", "1")
        calculation.set("calcMode", "auto")
        parts["xl/workbook.xml"] = serialize_xml(workbook, workbook_original)
        relationship_original = parts["xl/_rels/workbook.xml.rels"]
        relationships = parse_xml(relationship_original)
        removed: set[str] = set()
        for relationship in list(relationships):
            if relationship.get("Type", "").endswith("/calcChain"):
                target = relationship.get("Target", "")
                path = posixpath.normpath(posixpath.join("xl", target)).lstrip("/")
                removed.add(path)
                parts.pop(path, None)
                relationships.remove(relationship)
        if not removed:
            return
        parts["xl/_rels/workbook.xml.rels"] = serialize_xml(relationships, relationship_original)
        content_types_original = parts["[Content_Types].xml"]
        content_types = parse_xml(content_types_original)
        for override in list(content_types):
            if override.get("PartName", "").lstrip("/") in removed:
                content_types.remove(override)
        parts["[Content_Types].xml"] = serialize_xml(content_types, content_types_original)

    @staticmethod
    def _set_value(cell: ET.Element, value: str, *, text: bool) -> None:
        """替换单元格值，保留坐标和样式。

        Args:
            cell: 要写入的单元格。
            value: 已验证的文本或数值原始字符串。
            text: 是否以 inlineStr 写入；否则保持数值类型。
        """
        for child in list(cell):
            if child.tag in {tag("v"), tag("f"), tag("is")}:
                cell.remove(child)
        cell.set("t", "inlineStr" if text else "n")
        if text:
            ET.SubElement(ET.SubElement(cell, tag("is")), tag("t")).text = value
        else:
            ET.SubElement(cell, tag("v")).text = value

    @staticmethod
    def _insert_cell(root: ET.Element, coordinate: str, row_number: int) -> ET.Element:
        """为原合并区域中的空日期建立显式单元格。

        Args:
            root: 工作表根节点。
            coordinate: 目标坐标。
            row_number: 目标原始行号。

        Returns:
            按列顺序插入的单元格。
        """
        data = root.find(tag("sheetData"))
        if data is None:
            raise PreparationError("INVALID_WORKBOOK", "工作表缺少 sheetData")
        row = next((node for node in data if node.get("r") == str(row_number)), None)
        if row is None:
            raise PreparationError("INVALID_WORKBOOK", "明细行结构不存在")
        cell = ET.Element(tag("c"), {"r": coordinate})
        column = coordinate.rstrip("0123456789")
        order = (len(column), column)
        for index, sibling in enumerate(row):
            sibling_column = sibling.get("r", "").rstrip("0123456789")
            if (len(sibling_column), sibling_column) > order:
                row.insert(index, cell)
                break
        else:
            row.append(cell)
        return cell

    @staticmethod
    def _clear_fill(
        cell: ET.Element, styles: ET.Element, mapping: dict[str, str], *, numeric_format: bool
    ) -> None:
        """派生去底色样式，保留字体、边框、数字格式和对齐。

        Args:
            cell: 要更新样式引用的单元格。
            styles: 原样式表根节点。
            mapping: 已生成的新旧样式映射，避免重复派生。
            numeric_format: 是否将数值业务列设为 General，避免隐藏原始精度。
        """
        xfs = styles.find(tag("cellXfs"))
        if xfs is None:
            return
        old = cell.get("s", "0")
        key = f"{old}:{numeric_format}"
        if key not in mapping:
            new = deepcopy(xfs[int(old)])
            new.set("fillId", "0")
            new.set("applyFill", "1")
            if numeric_format:
                new.set("numFmtId", "0")
                new.set("applyNumberFormat", "1")
            mapping[key] = str(len(xfs))
            xfs.append(new)
            xfs.set("count", str(len(xfs)))
        cell.set("s", mapping[key])

    @staticmethod
    def _month_title(month: int) -> str:
        """返回中文月份标题。

        Args:
            month: 已验证为 1 至 12 的月份。

        Returns:
            例如“七月”的标题。
        """
        return ("一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二")[
            month - 1
        ] + "月"
