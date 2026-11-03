"""只描述表头，不包含明细行的工作簿结构。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class HeaderModel(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class MergedHeader(HeaderModel):
    range: str = Field(description="原合并区域，例如 A1:B2")
    source: str = Field(description="拆分后各单元格内容的来源坐标，例如 A1")


class HeaderColumn(HeaderModel):
    index: int = Field(ge=1, description="从 1 开始的原始列号")
    letter: str = Field(description="Excel 列字母")
    header_path: list[str] = Field(description="从上到下的表头层级，去除空值和连续重复值")


class SheetHeaders(HeaderModel):
    name: str
    header_start_row: int | None = Field(description="表头起始行，空表为 null")
    header_end_row: int | None = Field(description="表头结束行，空表为 null")
    column_count: int = Field(ge=0)
    header_rows: list[list[str | None]] = Field(
        description="合并表头展开后的二维矩阵，仅包含选定的表头行"
    )
    columns: list[HeaderColumn]
    merged_ranges: list[MergedHeader] = Field(description="仅记录与表头相交的原合并区域")


class WorkbookHeaders(HeaderModel):
    schema_version: Literal["1.0"] = "1.0"
    file_id: str
    filename: str
    sheets: list[SheetHeaders]
