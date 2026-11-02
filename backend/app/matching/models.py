"""匹配领域契约；原始数值与分配值分别保存，禁止隐式类型转换。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from app.preparation.domain.models import (
    ArtifactRef,
    BusinessRecord,
    Direction,
    DomainModel,
    Issue,
)

RowStatus = Literal["matched", "matched_review", "partial", "unmatched", "unresolved", "anomaly"]
RelationType = Literal["one_to_one", "one_to_many", "many_to_one", "partial"]


class MatchingRules(DomainModel):
    """版本化的比较口径；所有未指定数值容差均为零。"""

    version: str = Field(default="matching-v1", min_length=1)
    date_tolerance_days: int = Field(default=0, ge=0, le=366)
    specification_aliases: dict[str, str] = Field(default_factory=dict)
    quantity_tolerance: str = "0"
    price_tolerance: str = "0"
    amount_tolerance: str = "0"
    search_budget: int = Field(default=2000, ge=1, le=100000)
    max_group_size: int = Field(default=6, ge=1, le=20)
    max_candidates: int = Field(default=100, ge=1, le=1000)
    max_model_rounds: int = Field(default=2, ge=0, le=5)
    max_tool_queries: int = Field(default=2, ge=0, le=5)

    @field_validator("quantity_tolerance", "price_tolerance", "amount_tolerance")
    @classmethod
    def validate_tolerance(cls, value: str) -> str:
        """校验明确提供的数值容差。

        Args:
            value: 精确十进制文本，零表示不允许差异。

        Returns:
            有限且非负的原始容差文本。
        """
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("容差必须是十进制数值文本") from exc
        if not parsed.is_finite() or parsed < 0:
            raise ValueError("容差必须有限且非负")
        return value


class Check(DomainModel):
    """一次程序比较；delta 保留真实差额，容差通过也不抹平差异。"""

    field: str
    passed: bool
    expected: str | None
    actual: str | None
    delta: str | None = None
    detail: str = ""


class Allocation(DomainModel):
    """一条 A/B 分配边；数量、A 金额、B 金额分别保存。"""

    a_record_id: str
    b_record_id: str
    quantity: str
    a_amount: str
    b_amount: str


class AliasSuggestion(DomainModel):
    """模型当前行检索建议，不写入全局已确认别名。"""

    source: str
    target: str
    evidence: tuple[str, ...]


class Candidate(DomainModel):
    """程序产生的不可变候选，候选本身不消耗 B 记录。"""

    candidate_id: str
    a_record_ids: tuple[str, ...]
    b_record_ids: tuple[str, ...]
    rule_id: str
    rule_version: str
    direction: Direction
    relation_type: RelationType
    allocations: tuple[Allocation, ...]
    checks: tuple[Check, ...]
    eligible: bool
    requires_advice: bool = False
    search_alias: AliasSuggestion | None = None
    review_reasons: tuple[str, ...] = ()
    evidence: tuple[str, ...]
    tier: Literal["strict", "review", "partial", "rejected"]


class ModelProposal(DomainModel):
    """模型只能引用候选和已有来源，不能提交计算结果或排除业务。"""

    candidate_id: str | None = None
    specification_equivalent: bool = False
    reason: str = Field(min_length=1)
    evidence: tuple[str, ...] = ()
    suggested_alias: AliasSuggestion | None = None


class RowContext(DomainModel):
    """模型及行工作流的有界上下文。"""

    a_record: BusinessRecord
    neighbors: tuple[BusinessRecord, ...]
    b_records: tuple[BusinessRecord, ...]
    candidates: tuple[Candidate, ...]
    rules: MatchingRules
    b_usage: tuple[str, ...] = ()
    confirmations: tuple[MatchConfirmationRecord, ...] = ()


class ProposalTrace(DomainModel):
    """保存真实模型输入、输出、程序拒绝原因和提示词版本。"""

    round: int
    context: RowContext
    proposal: ModelProposal | None = None
    error: str | None = None
    prompt_id: str
    prompt_version: str
    prompt_sha256: str


class AliasQueryProgress(DomainModel):
    """当前行语义查询的独立游标，避免混用主规则搜索范围。"""

    alias: AliasSuggestion
    cursor: int
    search_complete: bool
    incomplete_reasons: tuple[str, ...]


class RowProgress(DomainModel):
    """行级幂等检查点；游标只对同一数据和规则版本有效。"""

    a_record_id: str
    cursor: int = 0
    search_scope: Literal["strict_single", "expanded"] = "expanded"
    base_search_complete: bool = False
    search_complete: bool = False
    search_incomplete_reasons: tuple[str, ...] = ()
    candidates: tuple[Candidate, ...] = ()
    proposals: tuple[ProposalTrace, ...] = ()
    selected_candidate_id: str | None = None
    tool_queries: int = 0
    alias_queries: tuple[AliasQueryProgress, ...] = ()
    execution_issues: tuple[Issue, ...] = ()


class MatchGroup(DomainModel):
    """经过源值、方向及容量校验后提交的正式关系。"""

    group_id: str
    candidate: Candidate
    status: Literal["matched", "matched_review", "partial"]
    review_reasons: tuple[str, ...]
    confirmation_refs: tuple[str, ...] = ()


class ARowResult(DomainModel):
    """每条 A 明细恰好一个结果；候选引用与正式引用分离。"""

    a_record_id: str
    status: RowStatus
    match_group_ids: tuple[str, ...] = ()
    candidate_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    remaining_quantity: str | None
    remaining_amount: str | None
    progress_ref: str


class BUsage(DomainModel):
    """仅从正式匹配组推导的 B 容量及剩余量。"""

    b_record_id: str
    group_ids: tuple[str, ...]
    allocated_quantity: str
    allocated_amount: str
    remaining_quantity: str | None
    remaining_amount: str | None
    excluded: bool


class ReviewItem(DomainModel):
    code: str
    a_record_ids: tuple[str, ...] = ()
    b_record_ids: tuple[str, ...] = ()
    candidate_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...]


class ManualDecision(DomainModel):
    """人工接受/释放关系、排除/恢复 B 或确认当前任务规则。"""

    action: Literal["accept", "release", "exclude_b", "restore_b", "rules"]
    candidate_id: str | None = None
    a_record_ids: tuple[str, ...] = ()
    b_record_ids: tuple[str, ...] = ()
    rules: MatchingRules | None = None

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        """拒绝缺少处理对象的人工请求。

        Returns:
            具有明确处理目标的决策。
        """
        if self.action == "accept" and not self.candidate_id:
            raise ValueError("接受关系必须指定候选")
        if self.action == "release" and not self.a_record_ids:
            raise ValueError("释放关系必须指定 A 行")
        if self.action in {"exclude_b", "restore_b"} and not self.b_record_ids:
            raise ValueError("排除或恢复必须指定 B 行")
        if (self.action == "rules") != (self.rules is not None):
            raise ValueError("规则更新必须且只能在 rules 操作中提供")
        return self


class MatchConfirmation(DomainModel):
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    expected_match_plan_version: int = Field(ge=1)
    decision: ManualDecision

    @field_validator("actor", "reason")
    @classmethod
    def nonblank(cls, value: str) -> str:
        """确保确认人和业务原因具有可审计文字。

        Args:
            value: 请求中提供的人员或原因。

        Returns:
            去除空白后的非空文字。
        """
        if not value.strip():
            raise ValueError("确认人及业务原因不能为空")
        return value.strip()


class MatchConfirmationRecord(DomainModel):
    confirmation_id: str
    confirmation: MatchConfirmation
    confirmed_at: datetime
    output_version: int


class Exclusion(DomainModel):
    b_record_id: str
    reason: str
    confirmation_ref: str


class MatchPlan(DomainModel):
    """第三块的唯一正式入口，所有引用绑定同一匹配方案版本。"""

    schema_version: Literal["1.0"] = "1.0"
    task_id: str
    prepared_data_ref: ArtifactRef
    prepared_data_version: int
    preparation_rule_version: str
    rule_version: str
    rules: MatchingRules
    match_plan_version: int
    a_row_results_ref: ArtifactRef
    match_groups_ref: ArtifactRef
    b_usage_ref: ArtifactRef
    review_items_ref: ArtifactRef
    exclusions_ref: ArtifactRef
    execution_issues_ref: ArtifactRef
    confirmations_ref: ArtifactRef
    progress_ref: ArtifactRef
    readiness: Literal["ready", "needs_input", "failed"]


class MatchArtifacts(DomainModel):
    """同一提交的完整结果快照，用于原子持久化和独立读取。"""

    match_plan_version: int
    rows: tuple[ARowResult, ...]
    groups: tuple[MatchGroup, ...]
    b_usage: tuple[BUsage, ...]
    reviews: tuple[ReviewItem, ...]
    exclusions: tuple[Exclusion, ...]
    execution_issues: tuple[Issue, ...]


class MatchingState(DomainModel):
    """原子任务头；最后才更新此文件发布新方案，失败可恢复工作版本。"""

    task_id: str
    prepared_data_ref: ArtifactRef
    data_version: int
    rules: MatchingRules
    version: int = 1
    status: Literal["running", "ready", "needs_input", "failed"] = "running"
    completed_rows: int = 0
    total_rows: int
    progress: tuple[RowProgress, ...] = ()
    pending_rows: tuple[str, ...] = ()
    confirmations: tuple[MatchConfirmationRecord, ...] = ()
    exclusions: tuple[Exclusion, ...] = ()
    plan_ref: ArtifactRef | None = None
    issues: tuple[Issue, ...] = ()


class RowsArtifact(DomainModel):
    rows: tuple[ARowResult, ...]


class GroupsArtifact(DomainModel):
    groups: tuple[MatchGroup, ...]


class UsageArtifact(DomainModel):
    records: tuple[BUsage, ...]


class ReviewsArtifact(DomainModel):
    items: tuple[ReviewItem, ...]


class ExclusionsArtifact(DomainModel):
    records: tuple[Exclusion, ...]


class IssuesArtifact(DomainModel):
    issues: tuple[Issue, ...]


class ConfirmationsArtifact(DomainModel):
    records: tuple[MatchConfirmationRecord, ...]


class ProgressArtifact(DomainModel):
    rows: tuple[RowProgress, ...]
