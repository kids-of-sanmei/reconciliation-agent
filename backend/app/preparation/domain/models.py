"""第一块的不可变数据契约；来源数值始终以原始十进制文本交接。"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DomainModel(BaseModel):
    """禁止额外字段、冻结实例，并在 JSON 边界执行严格校验。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class FileRole(StrEnum):
    A = "a"
    B = "b"
    REFERENCE = "reference"


class Direction(StrEnum):
    IN = "in"
    OUT = "out"
    UNKNOWN = "unknown"


class TaskStatus(StrEnum):
    RECEIVED = "received"
    PARSING = "parsing"
    IDENTIFYING = "identifying"
    NEEDS_INPUT = "needs_input"
    PREPARING = "preparing"
    READY = "ready"
    FAILED = "failed"


class FieldName(StrEnum):
    DATE = "date"
    SPEC = "spec"
    QUANTITY = "quantity"
    UNIT_PRICE = "unit_price"
    AMOUNT = "amount"
    DOCUMENT = "document"
    PRODUCT_ID = "product_id"
    PRODUCT_NAME = "product_name"
    WAREHOUSE = "warehouse"
    UNIT = "unit"
    NOTE = "note"
    IN_QUANTITY = "in_quantity"
    IN_PRICE = "in_price"
    IN_AMOUNT = "in_amount"
    OUT_QUANTITY = "out_quantity"
    OUT_PRICE = "out_price"
    OUT_AMOUNT = "out_amount"
    FIXED_COST = "fixed_cost"


class Issue(DomainModel):
    """机器可处理、人员可定位的问题。"""

    code: str
    message: str
    blocking: bool = True
    source_ref: str | None = None


class SourceFile(DomainModel):
    role: FileRole
    file_id: str
    original_name: str
    relative_path: str
    sha256: str
    size_bytes: int


class CellSnapshot(DomainModel):
    """保留 OOXML 中的原始值和公式缓存，不经过二进制浮点转换。"""

    coordinate: str
    row: int
    column: str
    kind: Literal["blank", "string", "number", "boolean", "date", "formula", "error"]
    value: str | None = None
    formula: str | None = None
    cached_value: str | None = None
    cached_type: str | None = None
    style_id: int = 0
    number_format: str = "General"


class SheetSnapshot(DomainModel):
    sheet_id: str
    name: str
    xml_path: str
    max_row: int
    cells: dict[str, CellSnapshot]
    merged_ranges: tuple[str, ...] = ()
    hidden_rows: tuple[int, ...] = ()
    hidden_columns: tuple[str, ...] = ()
    row_dimensions: tuple[str, ...] = ()
    column_dimensions: tuple[str, ...] = ()


class WorkbookSnapshot(DomainModel):
    source_file: SourceFile
    date_system: Literal["1900", "1904"]
    sheets: tuple[SheetSnapshot, ...]


class SheetMapping(DomainModel):
    """一张工作表的字段和业务范围，由模板、模型建议或人工提供。"""

    role: FileRole
    sheet_name: str
    header_row: int = Field(ge=1)
    data_start_row: int = Field(ge=1)
    data_end_row: int = Field(ge=1)
    fields: dict[FieldName, str]
    excluded_rows: tuple[int, ...] = ()

    @field_validator("fields")
    @classmethod
    def validate_columns(cls, value: dict[FieldName, str]) -> dict[FieldName, str]:
        """验证字段列标。

        Args:
            value: 业务字段到 Excel 列标的映射。

        Returns:
            标准化为大写且位于 Excel 列范围内的映射。
        """
        result: dict[FieldName, str] = {}
        for key, column in value.items():
            normalized = column.strip().upper()
            if not normalized.isascii() or not normalized.isalpha() or len(normalized) > 3:
                raise ValueError("列标必须是 A 至 XFD 之间的字母")
            number = 0
            for char in normalized:
                number = number * 26 + ord(char) - 64
            if number > 16384:
                raise ValueError("列标超出 Excel 范围")
            result[key] = normalized
        if len(set(result.values())) != len(result):
            raise ValueError("不同业务字段不能映射到同一列")
        return result

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        """检查表头、角色和明细范围。

        Returns:
            通过结构及范围检查的当前映射。
        """
        if self.role == FileRole.REFERENCE:
            raise ValueError("人工参考表只保留原始数据，不参与 A/B 业务字段映射")
        if not self.header_row < self.data_start_row <= self.data_end_row:
            raise ValueError("需要满足 表头行 < 明细起始行 <= 明细结束行")
        if any(row < self.data_start_row or row > self.data_end_row for row in self.excluded_rows):
            raise ValueError("排除行必须位于明细范围内")
        return self


class TaskOptions(DomainModel):
    """明确的处理配置；未配置日期容差不会在第一块擅自推定。"""

    reconciliation_period: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    mappings: tuple[SheetMapping, ...] = ()
    specification_aliases: dict[str, str] = Field(default_factory=dict)
    date_tolerance_days: int = Field(default=0, ge=0, le=366)

    @field_validator("reconciliation_period")
    @classmethod
    def validate_period(cls, value: str | None) -> str | None:
        """验证实际年月。

        Args:
            value: YYYY-MM 格式的任务期间，缺失时允许等待确认。

        Returns:
            可构成真实日历日期的期间。
        """
        if value is not None:
            date(int(value[:4]), int(value[5:7]), 1)
        return value


class Confirmation(DomainModel):
    """补充参数时必须带人员和原因，公式补充值按来源单元格索引。"""

    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    options: TaskOptions | None = None
    formula_values: dict[str, str] = Field(default_factory=dict)
    expected_data_version: int | None = Field(default=None, ge=1)

    @field_validator("actor", "reason")
    @classmethod
    def validate_audit_text(cls, value: str) -> str:
        """排除只有空白的人员或原因。

        Args:
            value: 人员名称或确认原因。

        Returns:
            去掉首尾空白且非空的审计文字。
        """
        result = value.strip()
        if not result:
            raise ValueError("确认人及原因不能只包含空白")
        return result


class ConfirmationRecord(DomainModel):
    """保存确认内容、发生时间及版本，支持审计和中断重放去重。"""

    confirmation: Confirmation
    confirmed_at: datetime
    input_data_version: int
    output_data_version: int


class TaskView(DomainModel):
    task_id: str
    status: TaskStatus
    stage: str
    created_at: datetime
    updated_at: datetime
    data_version: int = 1
    sources: tuple[SourceFile, ...]
    options: TaskOptions
    formula_values: dict[str, str] = Field(default_factory=dict)
    confirmations: tuple[ConfirmationRecord, ...] = ()
    issues: tuple[Issue, ...] = ()
    prepared_ref: str | None = None
    artifact_names: tuple[str, ...] = ()


class SourceLocation(DomainModel):
    file_id: str
    sheet_id: str
    sheet_name: str
    row: int
    cells: dict[FieldName, str]


class NormalizedFields(DomainModel):
    date: str | None
    spec: str | None
    quantity: str | None
    unit_price: str | None
    amount: str | None
    document: str | None = None
    product_id: str | None = None
    product_name: str | None = None
    warehouse: str | None = None
    unit: str | None = None
    note: str | None = None
    fixed_cost: str | None = None

    def with_numeric_values(
        self, quantity: str | None, unit_price: str | None, amount: str | None
    ) -> NormalizedFields:
        """生成带明确业务数值的新对象，避免使用无类型字段更新字典。

        Args:
            quantity: 该方向或 A 行的精确数量文本。
            unit_price: 对应来源单价，缺失时保留 None。
            amount: 对应来源金额，缺失时保留 None。

        Returns:
            保留其余字段并重新通过构造校验的标准化对象。
        """
        return NormalizedFields(
            date=self.date,
            spec=self.spec,
            quantity=quantity,
            unit_price=unit_price,
            amount=amount,
            document=self.document,
            product_id=self.product_id,
            product_name=self.product_name,
            warehouse=self.warehouse,
            unit=self.unit,
            note=self.note,
            fixed_cost=self.fixed_cost,
        )


class BusinessRecord(DomainModel):
    record_id: str
    b_row_id: str | None = None
    source: SourceLocation
    raw_fields: dict[FieldName, CellSnapshot]
    normalized_fields: NormalizedFields
    direction: Direction
    in_period: bool
    context: dict[str, str] = Field(default_factory=dict)
    issues: tuple[Issue, ...] = ()


class RecordSet(DomainModel):
    records: tuple[BusinessRecord, ...]


class SourceSnapshots(DomainModel):
    workbooks: tuple[WorkbookSnapshot, ...]


class ArtifactRef(DomainModel):
    name: str
    relative_path: str
    sha256: str


class BaselineTotals(DomainModel):
    sheet_name: str
    detail_count: int
    quantity: str
    amount: str


class TaskContext(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str
    reconciliation_period: str
    files: tuple[SourceFile, ...]
    field_mappings: tuple[SheetMapping, ...]
    rule_version: str = "preparation-v1"
    rules: TaskOptions
    data_version: int
    confirmations: tuple[ConfirmationRecord, ...]


class PreparedData(DomainModel):
    task_context: TaskContext
    source_snapshot_ref: ArtifactRef
    table_profiles_ref: ArtifactRef
    a_records_ref: ArtifactRef
    b_records_ref: ArtifactRef
    b_index_ref: ArtifactRef
    c_baseline_ref: ArtifactRef
    a_baseline_totals: tuple[BaselineTotals, ...]
    preparation_issues: tuple[Issue, ...]
    readiness: Literal["ready"] = "ready"


class SheetProfile(DomainModel):
    role: FileRole
    name: str
    max_row: int
    merged_ranges: tuple[str, ...]
    sample_rows: dict[str, dict[str, str]]


class TableProfiles(DomainModel):
    sheets: tuple[SheetProfile, ...]


class MappingAdvice(DomainModel):
    mappings: tuple[SheetMapping, ...]
    explanation: str


class LookupIndex(DomainModel):
    """所有键均指向记录标识列表，内容相同的来源记录也保留多个标识。"""

    by_direction: dict[str, tuple[str, ...]]
    by_date: dict[str, tuple[str, ...]]
    by_document: dict[str, tuple[str, ...]]
    by_product: dict[str, tuple[str, ...]]
    by_spec: dict[str, tuple[str, ...]]
    by_quantity: dict[str, tuple[str, ...]]


class AuditEvent(DomainModel):
    action: str
    source_ref: str
    before: str | None
    after: str | None


class AuditTrail(DomainModel):
    events: tuple[AuditEvent, ...]
