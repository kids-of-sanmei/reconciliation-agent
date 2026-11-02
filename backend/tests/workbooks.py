"""用独立 Excel 库生成测试输入，验证生产 OOXML 适配器的互操作性。"""

import io
from datetime import datetime
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook
from openpyxl.styles import Border, Font, PatternFill, Side
from openpyxl.utils.datetime import CALENDAR_MAC_1904

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def a_workbook(
    *, formula: bool = False, unknown_headers: bool = False, epoch_1904: bool = False
) -> bytes:
    """创建含合并日期、退货、零金额、样式及附件表的 A 输入。

    Args:
        formula: 第一条金额是否使用没有缓存的公式。
        unknown_headers: 是否使用需要人工或模型识别的陌生字段名。
        epoch_1904: 是否使用 1904 日期系统。

    Returns:
        可由真实 Excel 库打开的工作簿字节。
    """
    workbook = Workbook()
    if epoch_1904:
        workbook.epoch = CALENDAR_MAC_1904
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "业务底稿"
    sheet.append(["七月"])
    sheet.merge_cells("A1:E1")
    sheet.append(
        ["日期", "产品名称", "数量", "单价", "金额"]
        if not unknown_headers
        else ["字段甲", "字段乙", "字段丙", "字段丁", "字段戊"]
    )
    sheet.append(
        [datetime(2026, 7, 1), "支架站脚款", 462, 15.769826, "=C3*D3" if formula else 7285.659612]
    )
    sheet.append([None, "补发件", 0, 0, 0])
    sheet.merge_cells("A3:A4")
    sheet.append(["7月2日", "支架滑轮款", -3, 16.716, -50.148])
    sheet.append(["合计", None, "=SUM(C3:C5)", None, "=SUM(E3:E5)"])
    sheet.column_dimensions["B"].width = 28
    sheet.row_dimensions[4].hidden = True
    sheet["B3"].font = Font(name="等线", bold=True)
    sheet["B3"].border = Border(bottom=Side(style="thin", color="000000"))
    for row in sheet:
        for cell in row:
            cell.fill = PatternFill("solid", fgColor="FFFF00")
    sheet["D3"].number_format = "0.00"
    sheet["E3"].number_format = "0.###############"
    attachment = workbook.create_sheet("原始附件")
    attachment["A1"] = "完整保留此工作表"
    attachment["B2"] = 123
    attachment.merge_cells("A3:B3")
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def b_workbook() -> bytes:
    """创建入库、出库、重复内容、跨月和双向系统记录。

    Returns:
        包含已知 B 表 E—P 字段的工作簿字节。
    """
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "系统出入库"
    sheet.append(
        [
            "序号",
            "公司",
            "部门",
            "人员",
            "仓库",
            "系统出入库时间",
            "单据编号",
            "货品编号",
            "货品名称",
            "规格",
            "入库数量",
            "入库成本单价",
            "入库成本金额",
            "出库数量",
            "出库成本单价",
            "出库成本金额",
            "固定成本价",
        ]
    )
    first = [
        1,
        "公司",
        "部门",
        "经办",
        "配货仓",
        datetime(2026, 7, 1, 12, 30),
        "DOC01",
        "SKU01",
        "支架",
        "支架站脚",
        449,
        15.769826,
        7080.651874,
        0,
        0,
        0,
        16.716,
    ]
    sheet.append(first)
    sheet.append(
        [
            2,
            "公司",
            "部门",
            "经办",
            "补发仓",
            datetime(2026, 7, 1),
            "DOC02",
            "SKU02",
            "配件",
            "补发件",
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        ]
    )
    sheet.append(
        [
            3,
            "公司",
            "部门",
            "经办",
            "退货仓",
            datetime(2026, 7, 2),
            "DOC03",
            "SKU03",
            "支架",
            "支架滑轮",
            0,
            0,
            0,
            3,
            16.716,
            50.148,
            16.716,
        ]
    )
    sheet.append(first)
    sheet.append(
        [
            4,
            "公司",
            "部门",
            "经办",
            "配货仓",
            datetime(2026, 8, 1),
            "DOC04",
            "SKU04",
            "配件",
            "跨月",
            8,
            2,
            16,
            0,
            0,
            0,
            2,
        ]
    )
    sheet.append(
        [
            5,
            "公司",
            "部门",
            "经办",
            "配货仓",
            datetime(2026, 7, 3),
            "DOC05",
            "SKU05",
            "配件",
            "双向",
            1,
            2,
            2,
            2,
            2,
            4,
            2,
        ]
    )
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def replace_cell_xml(content: bytes, coordinate: str, *, value: str, cached: bool = False) -> bytes:
    """修改测试来源单元格的原始数字文本或公式缓存。

    Args:
        content: 输入工作簿。
        coordinate: 第一张表中的单元格坐标。
        value: 要保存的确切 XML 数值文本。
        cached: 是否保留公式并仅设置缓存。

    Returns:
        使用原始数值文本的工作簿字节。
    """
    buffer = io.BytesIO()
    with ZipFile(io.BytesIO(content)) as source, ZipFile(buffer, "w", ZIP_DEFLATED) as output:
        for info in source.infolist():
            data = source.read(info)
            if info.filename == "xl/worksheets/sheet1.xml":
                root = ET.fromstring(data)
                cell = next(
                    node for node in root.iter(f"{{{MAIN_NS}}}c") if node.get("r") == coordinate
                )
                if not cached:
                    for child in list(cell):
                        cell.remove(child)
                node = cell.find(f"{{{MAIN_NS}}}v")
                if node is None:
                    node = ET.SubElement(cell, f"{{{MAIN_NS}}}v")
                node.text = value
                cell.set("t", "n")
                data = ET.tostring(root, encoding="utf-8")
            output.writestr(info, data)
    return buffer.getvalue()
