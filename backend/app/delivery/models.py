"""制表及交付的严格类型契约，业务复核与系统验收分别表达。"""

from datetime import datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from app.matching.models import MatchArtifacts, MatchPlan, ReviewItem
from app.preparation.domain.models import (
    ArtifactRef,
    BusinessRecord,
    DomainModel,
    FieldName,
    PreparedData,
    SourceSnapshots,
    WorkbookSnapshot,
)

CheckStatus = Literal["pass", "fail", "not_run"]
DeliveryStatus = Literal["running", "completed", "completed_with_review", "needs_input", "failed"]


class DeliveryOptions(DomainModel):
    """明确的执行能力和重试预算；未执行的强制检查不能降低为通过。"""

    max_auto_repairs: int = Field(default=1, ge=0, le=3)
    formula_engine: Literal["builtin", "unavailable"] = "builtin"
    display_engine: Literal["bounds", "unavailable"] = "bounds"


class DeliveryRequest(DomainModel):
    expected_match_plan_version: int = Field(ge=1)
    options: DeliveryOptions = Field(default_factory=DeliveryOptions)
    force: bool = False


class DeliveryInputs(DomainModel):
    """冻结并重新校验后的生成输入，源文件本体保持不可变。"""

    prepared: PreparedData
    plan: MatchPlan
    plan_ref: ArtifactRef
    matches: MatchArtifacts
    snapshots: SourceSnapshots
    baseline: WorkbookSnapshot
    baseline_path: Path
    b_source_path: Path
    a_records: tuple[BusinessRecord, ...]
    b_records: tuple[BusinessRecord, ...]


class TraceColumn(DomainModel):
    field: FieldName
    header: str
    source_sheets: tuple[str, ...]


class SheetLayout(DomainModel):
    """原始业务列后追加的元数据和来源字段位置。"""

    sheet_name: str
    header_row: int
    original_columns: int
    original_max_row: int
    row_map: dict[str, int]
    metadata: dict[str, str]
    trace_columns: tuple[TraceColumn, ...]


class FormulaProblem(DomainModel):
    sheet_name: str
    coordinate: str
    code: str
    detail: str


class GenerationManifest(DomainModel):
    task_id: str
    data_version: int
    match_plan_version: int
    generation_version: int
    layouts: tuple[SheetLayout, ...]
    audit_sheet: str
    allocation_sheet: str
    review_sheet: str
    formula_problems: tuple[FormulaProblem, ...] = ()
    engine_version: Literal["ooxml-delivery-v1"] = "ooxml-delivery-v1"


class VerificationIssue(DomainModel):
    code: str
    stage: Literal["preparation", "matching", "delivery", "input"]
    message: str
    a_record_id: str | None = None
    b_record_id: str | None = None
    output_location: str | None = None
    expected: str | None = None
    actual: str | None = None
    evidence: tuple[str, ...] = ()
    repairable: bool = False


class VerificationCheck(DomainModel):
    name: str
    status: CheckStatus
    mandatory: bool = True
    detail: str


class ActualBTrace(DomainModel):
    """仅包含从实际 C 单元格验证通过的 B 承接事实。"""

    b_record_id: str
    group_id: str
    c_sheet: str
    c_row: int
    allocated_quantity: str
    allocated_amount: str


class CInspection(DomainModel):
    traces: tuple[ActualBTrace, ...]
    issues: tuple[VerificationIssue, ...]
    checks: tuple[VerificationCheck, ...]


class BCoverage(DomainModel):
    b_record_id: str
    source_sheet: str
    source_row: int
    direction: str
    in_period: bool
    disposition: Literal["taken", "missing", "excluded", "outside_period", "unknown_period"]
    missing_type: str
    explanation: str
    allocated_quantity: str
    allocated_amount: str
    remaining_quantity: str | None
    remaining_amount: str | None
    c_references: tuple[str, ...]
    a_record_ids: tuple[str, ...]
    group_ids: tuple[str, ...]
    exclusion_reason: str | None = None
    confirmation_ref: str | None = None


class SheetTotals(DomainModel):
    sheet_name: str
    a_quantity: str
    c_quantity: str
    quantity_difference: str
    a_amount: str
    c_amount: str
    amount_difference: str


class DeliveryStatistics(DomainModel):
    a_detail_count: int
    b_current_month_original_rows: int
    b_current_month_business_records: int
    taken_business_records: int
    missing_business_records: int
    excluded_business_records: int
    outside_period_business_records: int
    unknown_period_business_records: int
    row_status_counts: dict[str, int]
    totals: tuple[SheetTotals, ...]


class VerificationReport(DomainModel):
    task_id: str
    data_version: int
    match_plan_version: int
    generation_version: int
    verified_at: datetime
    checks: tuple[VerificationCheck, ...]
    issues: tuple[VerificationIssue, ...]
    coverage: tuple[BCoverage, ...]
    statistics: DeliveryStatistics
    formula_engine: str
    display_engine: str
    passed: bool

    @model_validator(mode="after")
    def validate_passed(self) -> Self:
        """阻止以单独布尔标记绕过失败、未执行或缺少的强制验收项。

        Returns:
            检查明细、问题列表及通过标记一致的报告。
        """
        if self.passed and (
            self.issues
            or not self.checks
            or any(c.mandatory and c.status != "pass" for c in self.checks)
        ):
            raise ValueError("存在失败、未执行、缺少的检查或系统问题，不能宣称验收通过")
        return self


class DeliveryReviewItems(DomainModel):
    matching: tuple[ReviewItem, ...]
    b_business_items: tuple[BCoverage, ...]


class DeliveryArtifact(DomainModel):
    kind: Literal[
        "c_workbook",
        "annotated_b",
        "verification_json",
        "verification_readable",
        "reviews",
        "manifest",
    ]
    reference: ArtifactRef
    data_version: int
    match_plan_version: int
    generation_version: int


class DeliveryResult(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str
    data_version: int
    match_plan_version: int
    generation_version: int
    status: Literal["completed", "completed_with_review", "needs_input", "failed"]
    artifacts: tuple[DeliveryArtifact, ...]
    statistics: DeliveryStatistics
    verification_report_ref: ArtifactRef
    review_items_ref: ArtifactRef


class DeliveryEvent(DomainModel):
    sequence: int
    occurred_at: datetime
    stage: str
    detail: str


class DeliveryState(DomainModel):
    task_id: str
    data_version: int
    match_plan_version: int
    generation_version: int
    status: DeliveryStatus
    stage: str
    options: DeliveryOptions
    plan_ref: ArtifactRef
    attempt: int = 0
    events: tuple[DeliveryEvent, ...] = ()
    issues: tuple[VerificationIssue, ...] = ()
    result_ref: ArtifactRef | None = None
    diagnostic_refs: tuple[ArtifactRef, ...] = ()
