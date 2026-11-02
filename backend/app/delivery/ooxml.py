"""保留 ZIP 资产的 OOXML 修改工具，写入数值不经过二进制浮点。"""

import io
import posixpath
import re
import unicodedata
from bisect import bisect_left
from copy import deepcopy
from math import ceil
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from app.matching.numbers import number, text
from app.preparation.domain.errors import PreparationError
from app.preparation.domain.models import CellSnapshot
from app.preparation.infrastructure.xlsx import REL_NS, parse_xml, serialize_xml, tag

PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
METADATA_HEADERS: tuple[str, ...] = (
    "A来源标识",
    "匹配状态",
    "匹配依据",
    "匹配组",
    "B来源标识",
    "B原始工作表",
    "B原始行号",
    "匹配方向",
    "A已对应数量",
    "A已对应金额",
    "A未结数量",
    "A未结金额",
    "B已对应数量",
    "B已对应金额",
    "B未结数量",
    "B未结金额",
)
B_HEADERS: tuple[str, ...] = (
    "C表缺失标注",
    "缺失类型",
    "缺失方向",
    "数量余额",
    "金额余额",
    "缺失匹配依据",
    "标注说明",
    "相关A标识",
    "相关C行号",
    "匹配组",
    "人工参考表行号",
    "人工排除原因",
    "确认引用",
    "B方向记录标识",
)
STATUS_LABELS: dict[str, str] = {
    "matched": "已匹配",
    "matched_review": "已对应/需复核",
    "partial": "部分匹配",
    "unmatched": "未匹配",
    "unresolved": "待确认",
    "anomaly": "业务异常",
}
DIRECTION_LABELS: dict[str, str] = {"in": "入库", "out": "出库", "unknown": "方向待复核"}
COLORS: dict[str, str] = {
    "matched": "E8F2E9",
    "matched_review": "FFF2CC",
    "partial": "FCE4D6",
    "unmatched": "FCE8E6",
    "unresolved": "FFF2CC",
    "anomaly": "FCE8E6",
    "header": "DDEBF7",
}


def column(number_value: int) -> str:
    """把合法的 Excel 列编号转换为列标。

    Args:
        number_value: 从 1 开始的列编号。

    Returns:
        A 至 XFD 范围内的列标。
    """
    if not 1 <= number_value <= 16384:
        raise PreparationError("OUTPUT_COLUMN_LIMIT", "追加追溯列超出 Excel 列数限制")
    result = ""
    while number_value:
        number_value, remainder = divmod(number_value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def column_number(value: str) -> int:
    """转换列标以比较、追加列。

    Args:
        value: Excel 字母列标。

    Returns:
        从 1 开始的列编号。
    """
    result = 0
    for char in value:
        result = result * 26 + ord(char) - 64
    return result


def row_number(coordinate: str) -> int:
    """从已验证的单元格坐标读取行号。

    Args:
        coordinate: 带字母列标的完整单元格坐标。

    Returns:
        从 1 开始的行号。
    """
    return int("".join(char for char in coordinate if char.isdigit()))


def display_width(value: str) -> int:
    """为可验证显示能力估算保守的字符宽度上界。

    Args:
        value: 单元格待显示文本。

    Returns:
        按最宽一行及东亚宽字符折算的字符单元数。
    """
    return max(
        (
            sum(2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1 for c in line)
            for line in value.splitlines()
        ),
        default=0,
    )


class Package:
    """完整保留未修改工作簿部件，集中管理安全的单元格和样式修改。"""

    def __init__(self, path: Path) -> None:
        """读取已由来源层验证过大小和摘要的工作簿。

        Args:
            path: 当前冻结版本的底稿或 B 原始工作簿。
        """
        with ZipFile(path) as archive:
            self.parts: dict[str, bytes] = {
                info.filename: archive.read(info) for info in archive.infolist()
            }
        self.workbook = parse_xml(self.parts["xl/workbook.xml"])
        self.relationships = parse_xml(self.parts["xl/_rels/workbook.xml.rels"])
        self.content_types = parse_xml(self.parts["[Content_Types].xml"])
        self.styles = parse_xml(self.parts["xl/styles.xml"])
        self.style_cache: dict[tuple[int, str, str], int] = {}
        self.roots: dict[str, ET.Element] = {}
        self.row_maps: dict[int, dict[int, ET.Element]] = {}
        self.cell_maps: dict[int, dict[str, ET.Element]] = {}
        self.row_orders: dict[int, list[int]] = {}
        self.strings = (
            tuple(
                "".join(n.text or "" for n in item.iter(tag("t")))
                for item in parse_xml(self.parts["xl/sharedStrings.xml"])
            )
            if "xl/sharedStrings.xml" in self.parts
            else ()
        )

    def sheet_paths(self) -> dict[str, str]:
        """按工作簿关系解析工作表实际部件。

        Returns:
            工作表名称到 ZIP 内路径的映射。
        """
        targets = {
            node.get("Id", ""): posixpath.normpath(
                posixpath.join("xl", node.get("Target", ""))
            ).lstrip("/")
            for node in self.relationships
            if node.get("TargetMode") != "External"
        }
        return {
            sheet.get("name", ""): targets[sheet.get(f"{{{REL_NS}}}id", "")]
            for sheet in self.workbook.findall(f"{tag('sheets')}/{tag('sheet')}")
        }

    def sheet(self, name: str) -> ET.Element:
        """取得可编辑工作表根节点。

        Args:
            name: 已存在的工作表名称。

        Returns:
            缓存的可编辑 XML 树。
        """
        path = self.sheet_paths()[name]
        if path not in self.roots:
            self.roots[path] = parse_xml(self.parts[path])
        return self.roots[path]

    def add_sheet(self, preferred_name: str) -> tuple[str, ET.Element]:
        """追加独立审计或分配表，不覆盖原始同名附件。

        Args:
            preferred_name: 期望的人类可读名称。

        Returns:
            避免重名的实际名称和空工作表根节点。
        """
        names = set(self.sheet_paths())
        name = preferred_name
        suffix = 2
        while name in names:
            name = f"{preferred_name[:26]}_{suffix}"
            suffix += 1
        sheets = self.workbook.find(tag("sheets"))
        if sheets is None:
            raise PreparationError("INVALID_WORKBOOK", "工作簿缺少 sheets")
        identifier = max((int(s.get("sheetId", "0")) for s in sheets), default=0) + 1
        path = f"xl/worksheets/reconciliation_{identifier}.xml"
        while path in self.parts or path in self.roots:
            identifier += 1
            path = f"xl/worksheets/reconciliation_{identifier}.xml"
        relationship_id = f"reconciliationSheet{identifier}"
        while any(r.get("Id") == relationship_id for r in self.relationships):
            relationship_id += "x"
        ET.SubElement(
            self.relationships,
            f"{{{PACKAGE_REL}}}Relationship",
            {
                "Id": relationship_id,
                "Type": f"{REL_NS}/worksheet",
                "Target": path.removeprefix("xl/"),
            },
        )
        ET.SubElement(
            sheets,
            tag("sheet"),
            {"name": name, "sheetId": str(identifier), f"{{{REL_NS}}}id": relationship_id},
        )
        ET.SubElement(
            self.content_types,
            f"{{{CONTENT_NS}}}Override",
            {
                "PartName": "/" + path,
                "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
            },
        )
        root = ET.Element(tag("worksheet"))
        ET.SubElement(root, tag("sheetData"))
        self.roots[path] = root
        return name, root

    def cell(self, root: ET.Element, coordinate: str) -> ET.Element:
        """获取或按合法顺序插入单元格。

        Args:
            root: 目标工作表。
            coordinate: 完整 Excel 坐标。

        Returns:
            可设置值和样式的单元格节点。
        """
        match = re.fullmatch(r"([A-Z]+)([1-9]\d*)", coordinate)
        if match is None or int(match.group(2)) > 1048576:
            raise PreparationError("OUTPUT_ROW_LIMIT", "输出坐标超出 Excel 范围")
        data = root.find(tag("sheetData"))
        if data is None:
            raise PreparationError("INVALID_WORKBOOK", "工作表缺少 sheetData")
        row_number = int(match.group(2))
        root_id = id(root)
        if root_id not in self.row_maps:
            self.row_maps[root_id] = {int(r.get("r", "0")): r for r in data}
            self.cell_maps[root_id] = {c.get("r", ""): c for r in data for c in r}
            self.row_orders[root_id] = sorted(self.row_maps[root_id])
        row = self.row_maps[root_id].get(row_number)
        if row is None:
            row = ET.Element(tag("row"), {"r": str(row_number)})
            position = bisect_left(self.row_orders[root_id], row_number)
            data.insert(position, row)
            self.row_orders[root_id].insert(position, row_number)
            self.row_maps[root_id][row_number] = row
        existing = self.cell_maps[root_id].get(coordinate)
        if existing is not None:
            return existing
        cell = ET.Element(tag("c"), {"r": coordinate})
        position = next(
            (
                i
                for i, c in enumerate(row)
                if column_number(c.get("r", "A1").rstrip("0123456789"))
                > column_number(match.group(1))
            ),
            len(row),
        )
        row.insert(position, cell)
        self.cell_maps[root_id][coordinate] = cell
        return cell

    def style(self, old: int = 0, *, color: str = "", number_format: str = "General") -> int:
        """派生样式，保留来源字体及边框，明确数字格式和状态底色。

        Args:
            old: 原单元格样式编号。
            color: 无井号的 RGB 底色，空文本表示清除底色。
            number_format: 完整数字格式，默认 General。

        Returns:
            当前输出工作簿内有效的新样式编号。
        """
        key = (old, color, number_format)
        if key in self.style_cache:
            return self.style_cache[key]
        xfs, fills = self.styles.find(tag("cellXfs")), self.styles.find(tag("fills"))
        if xfs is None or fills is None:
            raise PreparationError("INVALID_STYLE", "来源样式缺少 cellXfs 或 fills")
        new = deepcopy(xfs[old])
        fill_id = 0
        if color:
            for i, fill in enumerate(fills):
                fg = fill.find(f"{tag('patternFill')}/{tag('fgColor')}")
                if fg is not None and fg.get("rgb") == "FF" + color:
                    fill_id = i
                    break
            else:
                fill_id = len(fills)
                fill = ET.SubElement(fills, tag("fill"))
                pattern = ET.SubElement(fill, tag("patternFill"), {"patternType": "solid"})
                ET.SubElement(pattern, tag("fgColor"), {"rgb": "FF" + color})
                ET.SubElement(pattern, tag("bgColor"), {"indexed": "64"})
                fills.set("count", str(len(fills)))
        format_id = 0
        if number_format == "0":
            format_id = 1
        elif number_format != "General":
            formats = self.styles.find(tag("numFmts"))
            if formats is None:
                formats = ET.Element(tag("numFmts"))
                self.styles.insert(0, formats)
            existing = next(
                (node for node in formats if node.get("formatCode") == number_format), None
            )
            if existing is not None:
                format_id = int(existing.get("numFmtId", "0"))
            else:
                format_id = max([163, *(int(n.get("numFmtId", "0")) for n in formats)]) + 1
                ET.SubElement(
                    formats,
                    tag("numFmt"),
                    {"numFmtId": str(format_id), "formatCode": number_format},
                )
                formats.set("count", str(len(formats)))
        new.set("fillId", str(fill_id))
        new.set("applyFill", "1")
        new.set("numFmtId", str(format_id))
        new.set("applyNumberFormat", "1")
        alignment = new.find(tag("alignment"))
        if alignment is not None:
            new.remove(alignment)
        ET.SubElement(new, tag("alignment"), {"vertical": "center", "wrapText": "1"})
        identifier = len(xfs)
        xfs.append(new)
        xfs.set("count", str(len(xfs)))
        self.style_cache[key] = identifier
        return identifier

    def write(
        self,
        root: ET.Element,
        coordinate: str,
        value: str | None,
        *,
        numeric: bool = False,
        color: str = "",
        number_format: str = "General",
        preserve_style: bool = False,
    ) -> None:
        """写入文本或精确数值，禁止把来源备注当成可执行公式。

        Args:
            root: 目标工作表。
            coordinate: 输出坐标。
            value: 原始文本或精确十进制文本，None 表示空白。
            numeric: 是否写成 OOXML 数值。
            color: 状态底色。
            number_format: 明确的数值显示格式。
            preserve_style: 是否以该单元格原样式为基础保留字体及边框。
        """
        cell = self.cell(root, coordinate)
        old = int(cell.get("s", "0")) if preserve_style else 0
        for child in list(cell):
            if child.tag in {tag("v"), tag("f"), tag("is")}:
                cell.remove(child)
        cell.set("s", str(self.style(old, color=color, number_format=number_format)))
        if value is None:
            cell.attrib.pop("t", None)
        elif numeric:
            number(value)
            cell.set("t", "n")
            ET.SubElement(cell, tag("v")).text = value
        else:
            if len(value) > 32767:
                raise PreparationError(
                    "CELL_TEXT_LIMIT", "来源或审计字段超过 Excel 单元格文本上限，不能截断后交付"
                )
            cell.set("t", "inlineStr")
            node = ET.SubElement(ET.SubElement(cell, tag("is")), tag("t"))
            node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            node.text = value

    def wrap_style(self, old: int) -> int:
        """仅派生可换行样式，保留既有来源字体、边框、数字格式及状态色。

        Args:
            old: 当前输出内已存在的样式编号。

        Returns:
            启用换行及垂直居中的新样式编号。
        """
        key = (old, "__wrap__", "__keep_format__")
        if key in self.style_cache:
            return self.style_cache[key]
        styles = self.styles.find(tag("cellXfs"))
        if styles is None:
            raise PreparationError("INVALID_STYLE", "缺少可派生的单元格样式")
        new = deepcopy(styles[old])
        alignment = new.find(tag("alignment"))
        if alignment is None:
            alignment = ET.SubElement(new, tag("alignment"))
        alignment.set("wrapText", "1")
        alignment.set("vertical", "center")
        alignment.attrib.pop("shrinkToFit", None)
        new.set("applyAlignment", "1")
        identifier = len(styles)
        styles.append(new)
        styles.set("count", str(len(styles)))
        self.style_cache[key] = identifier
        return identifier

    def copy_value(
        self, root: ET.Element, coordinate: str, source: CellSnapshot | None, *, color: str = ""
    ) -> None:
        """复制 B 的实际来源值和必要日期格式，不按数量乘单价改写金额。

        Args:
            root: C 目标工作表。
            coordinate: 追溯目标坐标。
            source: 原始来源单元格，公式只承接已有缓存。
            color: 当前匹配状态底色。
        """
        value = (
            None
            if source is None
            else source.cached_value
            if source.kind == "formula"
            else source.value
        )
        numeric = source is not None and source.kind in {"number", "formula"} and value is not None
        date_format = (
            source is not None
            and bool(re.search(r"[ymdhis]", source.number_format, re.IGNORECASE))
            and source.number_format != "General"
        )
        self.write(
            root,
            coordinate,
            value,
            numeric=numeric,
            color=color,
            number_format="yyyy-mm-dd hh:mm:ss" if numeric and date_format else "General",
        )

    def finish_sheet(
        self, root: ET.Element, header_row: int, last_column: int, last_row: int
    ) -> None:
        """复位顶部视图、冻结真实表头、设置筛选和保守可读尺寸。

        Args:
            root: 已写完的工作表。
            header_row: 真正字段表头行。
            last_column: 最后输出列编号。
            last_row: 最后输出业务或结构行。
        """
        for name in ("dimension", "sheetViews", "cols", "autoFilter", "conditionalFormatting"):
            for node in list(root.findall(tag(name))):
                root.remove(node)
        dimension = ET.Element(tag("dimension"), {"ref": f"A1:{column(last_column)}{last_row}"})
        views = ET.Element(tag("sheetViews"))
        view = ET.SubElement(views, tag("sheetView"), {"workbookViewId": "0", "topLeftCell": "A1"})
        ET.SubElement(
            view,
            tag("pane"),
            {
                "ySplit": str(header_row),
                "topLeftCell": f"A{header_row + 1}",
                "activePane": "topLeft",
                "state": "frozen",
            },
        )
        ET.SubElement(
            view, tag("selection"), {"pane": "topLeft", "activeCell": "A1", "sqref": "A1"}
        )
        columns = ET.Element(tag("cols"))
        widths: dict[int, int] = {}
        data = root.find(tag("sheetData"))
        if data is None:
            raise PreparationError("INVALID_WORKBOOK", "工作表缺少数据")
        row_contents: list[tuple[ET.Element, list[tuple[int, str, int]]]] = []
        fonts = self.styles.find(tag("fonts"))
        xfs = self.styles.find(tag("cellXfs"))
        custom_formats = {
            int(n.get("numFmtId", "0")): n.get("formatCode", "")
            for n in self.styles.findall(f"{tag('numFmts')}/{tag('numFmt')}")
        }
        for row in data:
            row.attrib.pop("hidden", None)
            row.attrib.pop("collapsed", None)
            contents: list[tuple[int, str, int]] = []
            for cell in row:
                cell.set("s", str(self.wrap_style(int(cell.get("s", "0")))))
                value = (
                    "".join(n.text or "" for n in cell.iter(tag("t")))
                    if cell.get("t") == "inlineStr"
                    else cell.findtext(tag("v"), "")
                )
                if cell.get("t") == "s":
                    value = self.strings[int(value)] if value else ""
                if cell.get("t", "n") == "n" and value:
                    value = text(number(value))
                style_id = int(cell.get("s", "0"))
                font_size = 11
                if fonts is not None and xfs is not None:
                    xf = xfs[style_id]
                    font = fonts[int(xf.get("fontId", "0"))]
                    size_node = font.find(tag("sz"))
                    font_size = (
                        ceil(number(size_node.get("val", "11"))) if size_node is not None else 11
                    )
                    if (
                        custom_formats.get(int(xf.get("numFmtId", "0"))) == "yyyy-mm-dd hh:mm:ss"
                        and value
                    ):
                        value = "2026-12-31 23:59:59"
                index = column_number(cell.get("r", "A1").rstrip("0123456789"))
                widths[index] = max(
                    widths.get(index, 0), display_width(value) * max(font_size, 11) // 11
                )
                contents.append((index, value, font_size))
            row_contents.append((row, contents))
        for index in range(1, last_column + 1):
            width = min(255, max(14, widths.get(index, 0) * 2 + 6))
            ET.SubElement(
                columns,
                tag("col"),
                {"min": str(index), "max": str(index), "width": str(width), "customWidth": "1"},
            )
        for row, contents in row_contents:
            required_height = 28
            for index, value, font_size in contents:
                fitted_width = min(255, max(14, widths.get(index, 0) * 2 + 6))
                characters_per_line = max(1, (fitted_width - 6) * 11 // max(1, font_size))
                line_count = sum(
                    max(1, (display_width(line) + characters_per_line - 1) // characters_per_line)
                    for line in value.splitlines() or [""]
                )
                required_height = max(required_height, (font_size + 9) * line_count)
            row.set("ht", str(min(409, required_height)))
            row.set("customHeight", "1")
        insertion = 0 if root.find(tag("sheetPr")) is None else 1
        root.insert(insertion, dimension)
        root.insert(insertion + 1, views)
        # sheetFormatPr 必须位于 sheetViews 与 cols 之间。
        data_index = list(root).index(data)
        root.insert(data_index, columns)
        after_data = list(root).index(data) + 1
        ET.SubElement(
            root,
            tag("autoFilter"),
            {"ref": f"A{header_row}:{column(last_column)}{max(header_row, last_row)}"},
        )
        filter_node = root.find(tag("autoFilter"))
        if filter_node is not None:
            root.remove(filter_node)
            root.insert(after_data, filter_node)

    def save(self) -> bytes:
        """输出完整工作簿并移除已过期的计算链。

        Returns:
            可独立重读的 xlsx 字节；图像、附件等未修改部件保持原样。
        """
        for path, root in self.roots.items():
            self.parts[path] = serialize_xml(root, self.parts.get(path, b""))
        for path, root in (
            ("xl/workbook.xml", self.workbook),
            ("xl/_rels/workbook.xml.rels", self.relationships),
            ("[Content_Types].xml", self.content_types),
            ("xl/styles.xml", self.styles),
        ):
            self.parts[path] = serialize_xml(root, self.parts[path])
        self.parts.pop("xl/calcChain.xml", None)
        for relationship in list(self.relationships):
            if relationship.get("Type", "").endswith("/calcChain"):
                self.relationships.remove(relationship)
        for item in list(self.content_types):
            if item.get("PartName", "").endswith("/calcChain.xml"):
                self.content_types.remove(item)
        self.parts["xl/_rels/workbook.xml.rels"] = serialize_xml(
            self.relationships, self.parts["xl/_rels/workbook.xml.rels"]
        )
        self.parts["[Content_Types].xml"] = serialize_xml(
            self.content_types, self.parts["[Content_Types].xml"]
        )
        buffer = io.BytesIO()
        with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
            for name, content in self.parts.items():
                archive.writestr(name, content)
        return buffer.getvalue()

    def activate(self, sheet_name: str) -> None:
        """把工作簿活动页定位到首个实际业务表，避免打开后停在原附件页。

        Args:
            sheet_name: 本次生成应默认显示的实际业务工作表。
        """
        names = list(self.sheet_paths())
        views = self.workbook.find(tag("bookViews"))
        if views is None:
            views = ET.Element(tag("bookViews"))
            sheets = self.workbook.find(tag("sheets"))
            self.workbook.insert(
                list(self.workbook).index(sheets) if sheets is not None else 0, views
            )
        view = views.find(tag("workbookView"))
        if view is None:
            view = ET.SubElement(views, tag("workbookView"))
        view.set("activeTab", str(names.index(sheet_name)))
        view.set("firstSheet", "0")
        for name in names:
            for sheet_view in self.sheet(name).findall(f"{tag('sheetViews')}/{tag('sheetView')}"):
                sheet_view.set("tabSelected", "1" if name == sheet_name else "0")
