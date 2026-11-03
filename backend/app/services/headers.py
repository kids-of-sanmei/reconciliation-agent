"""使用 Excel 读取库提取表头，在内存中展开合并区域。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from functools import partial
from pathlib import Path
from typing import Final, Literal

import xlrd
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from xlrd.sheet import Sheet

from app.schemas.headers import HeaderColumn, MergedHeader, SheetHeaders, WorkbookHeaders
from app.services.errors import InvalidExcelError, InvalidHeaderRangeError, UploadError

_MAX_HEADER_CELLS: Final[int] = 100_000


@dataclass(frozen=True, slots=True)
class _Merge:
    first_row: int
    last_row: int
    first_column: int
    last_column: int

    @property
    def source(self) -> str:
        return f"{get_column_letter(self.first_column)}{self.first_row}"

    @property
    def reference(self) -> str:
        return f"{self.source}:{get_column_letter(self.last_column)}{self.last_row}"


@dataclass(frozen=True, slots=True)
class _Sheet:
    name: str
    row_count: int
    column_count: int
    merges: list[_Merge]
    value_at: Callable[[int, int], object]


def _text(value: object) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return str(value)


def _xls_value(sheet: Sheet, datemode: Literal[0, 1], row: int, column: int) -> object:
    if row > sheet.nrows or column > sheet.ncols:
        return None
    cell = sheet.cell(row - 1, column - 1)
    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return None
    if cell.ctype == xlrd.XL_CELL_DATE:
        return xlrd.xldate_as_datetime(float(cell.value), datemode)
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)
    if cell.ctype == xlrd.XL_CELL_ERROR:
        return xlrd.error_text_from_code.get(int(cell.value), "#ERROR!")
    return cell.value


class ExcelHeaderReader:
    """两种格式使用同一表头结构，业务明细不会进入返回模型。"""

    def read(
        self,
        *,
        path: Path,
        file_id: str,
        filename: str,
        header_start_row: int | None = None,
        header_row_count: int | None = None,
    ) -> WorkbookHeaders:
        if header_start_row is not None and header_start_row < 1:
            raise InvalidHeaderRangeError("表头起始行必须大于 0")
        if header_row_count is not None and header_row_count < 1:
            raise InvalidHeaderRangeError("表头行数必须大于 0")
        try:
            if path.suffix.lower() == ".xlsx":
                sheets = self._read_xlsx(path, header_start_row, header_row_count)
            else:
                sheets = self._read_xls(path, header_start_row, header_row_count)
        except (UploadError, OSError):
            raise
        except Exception as error:
            # 读取库对损坏、加密及伪装文件可能抛出不同类型的异常。
            raise InvalidExcelError("无法读取 Excel，请检查文件是否损坏、加密或与扩展名不符") from error
        return WorkbookHeaders(file_id=file_id, filename=filename, sheets=sheets)

    def _read_xlsx(
        self, path: Path, start: int | None, count: int | None
    ) -> list[SheetHeaders]:
        workbook = load_workbook(path, data_only=False, keep_links=False)
        try:
            result: list[SheetHeaders] = []
            for sheet in workbook.worksheets:
                merges = [
                    _Merge(item.min_row, item.max_row, item.min_col, item.max_col)
                    for item in sheet.merged_cells.ranges
                ]

                def value_at(row: int, column: int) -> object:
                    return sheet.cell(row, column).value

                result.append(self._extract(
                    _Sheet(sheet.title, sheet.max_row, sheet.max_column, merges, value_at),
                    start, count,
                ))
            return result
        finally:
            workbook.close()

    def _read_xls(
        self, path: Path, start: int | None, count: int | None
    ) -> list[SheetHeaders]:
        workbook = xlrd.open_workbook(str(path), formatting_info=True, on_demand=True)
        try:
            result: list[SheetHeaders] = []
            for index in range(workbook.nsheets):
                sheet = workbook.sheet_by_index(index)
                merges = [
                    _Merge(first_row + 1, last_row, first_column + 1, last_column)
                    for first_row, last_row, first_column, last_column in sheet.merged_cells
                ]
                result.append(self._extract(
                    _Sheet(
                        sheet.name, sheet.nrows, sheet.ncols, merges,
                        partial(_xls_value, sheet, workbook.datemode),
                    ),
                    start, count,
                ))
                workbook.unload_sheet(index)
            return result
        finally:
            workbook.release_resources()

    def _extract(self, sheet: _Sheet, start: int | None, count: int | None) -> SheetHeaders:
        # 不根据数据内容猜测表头结束位置，避免把文字型明细当成表头保存。
        first_nonempty_row = next((
            row for row in range(1, sheet.row_count + 1)
            if any(_text(sheet.value_at(row, column)) is not None
                   for column in range(1, sheet.column_count + 1))
        ), None)
        if first_nonempty_row is None:
            return SheetHeaders(
                name=sheet.name, header_start_row=None, header_end_row=None,
                column_count=0, header_rows=[], columns=[], merged_ranges=[],
            )

        if start is None:
            start = first_nonempty_row

        end = start + (count or 1) - 1
        if start > sheet.row_count or end > sheet.row_count:
            raise InvalidHeaderRangeError(f"工作表“{sheet.name}”的表头范围超出实际行数")

        # 跨行合并的表头必须完整展开；再次检查以覆盖相邻的嵌套层级。
        while True:
            expanded_end = max(
                [end] + [item.last_row for item in sheet.merges
                         if item.first_row <= end and item.last_row >= start]
            )
            if expanded_end == end:
                break
            end = expanded_end

        header_merges = [item for item in sheet.merges
                         if item.first_row <= end and item.last_row >= start]
        header_height = end - start + 1
        column_count = max((item.last_column for item in header_merges), default=0)
        if header_height * max(column_count, 1) > _MAX_HEADER_CELLS:
            raise InvalidHeaderRangeError("表头范围过大，请缩小表头行数或检查合并区域")

        # max_column/ncols 包含仅有格式的空白单元格，不能作为表头列数。
        # 先收集真正有内容的表头，再补齐矩阵，避免把尾部空列写入 JSON。
        populated_rows: list[dict[int, str]] = []
        for row in range(start, end + 1):
            populated: dict[int, str] = {}
            for column in range(1, sheet.column_count + 1):
                value = _text(sheet.value_at(row, column))
                if value is None:
                    continue
                if column > column_count:
                    column_count = column
                    if header_height * column_count > _MAX_HEADER_CELLS:
                        raise InvalidHeaderRangeError("表头范围过大，请缩小表头行数或检查合并区域")
                populated[column] = value
            populated_rows.append(populated)

        rows = [
            [populated.get(column) for column in range(1, column_count + 1)]
            for populated in populated_rows
        ]
        merged_ranges: list[MergedHeader] = []
        for item in sorted(header_merges, key=lambda merge: (merge.first_row, merge.first_column)):
            value = _text(sheet.value_at(item.first_row, item.first_column))
            for row in range(max(start, item.first_row), min(end, item.last_row) + 1):
                for column in range(item.first_column, item.last_column + 1):
                    rows[row - start][column - 1] = value
            merged_ranges.append(MergedHeader(range=item.reference, source=item.source))

        columns: list[HeaderColumn] = []
        for index in range(column_count):
            path: list[str] = []
            for row_values in rows:
                label = row_values[index]
                if label is not None and (not path or path[-1] != label):
                    path.append(label)
            columns.append(HeaderColumn(
                index=index + 1, letter=get_column_letter(index + 1), header_path=path,
            ))
        return SheetHeaders(
            name=sheet.name, header_start_row=start, header_end_row=end,
            column_count=column_count, header_rows=rows, columns=columns,
            merged_ranges=merged_ranges,
        )
