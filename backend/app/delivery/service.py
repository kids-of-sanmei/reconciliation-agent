"""冻结输入、受限修复、独立验收及最终交付注册的应用服务。"""

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from filelock import Timeout

from app.core.config import Settings
from app.delivery.generate import CGenerator
from app.delivery.mark_b import mark_b
from app.delivery.models import (
    DeliveryArtifact,
    DeliveryEvent,
    DeliveryInputs,
    DeliveryRequest,
    DeliveryResult,
    DeliveryReviewItems,
    DeliveryState,
    GenerationManifest,
    VerificationCheck,
    VerificationIssue,
    VerificationReport,
)
from app.delivery.verify import FileVerifier, issue
from app.matching.models import ConfirmationsArtifact
from app.matching.service import MatchingService
from app.matching.validation import validate_groups
from app.preparation.domain.errors import PreparationError
from app.preparation.domain.models import (
    ArtifactRef,
    FileRole,
    PreparedData,
    RecordSet,
    SourceSnapshots,
)
from app.preparation.infrastructure.storage import LocalTaskRepository

logger = logging.getLogger(__name__)
FINAL_STATUSES = {"completed", "completed_with_review"}


def readable_report(report: VerificationReport) -> str:
    """根据真实统计生成可阅读报告，不调用模型编造数量或结论。

    Args:
        report: 已保存的独立验收结果。

    Returns:
        包含输入版本、能力、检查、差异和定位事项的 Markdown 文本。
    """
    statistics = report.statistics
    lines = [
        "# 对账制表与反向核查报告",
        "",
        f"任务：{report.task_id}",
        f"数据版本：{report.data_version}；匹配版本：{report.match_plan_version}；生成版本：{report.generation_version}",
        f"验收时间（UTC）：{report.verified_at.isoformat()}",
        "",
        "验收通过。" if report.passed else "强制验收尚未全部通过，文件仅供诊断或待修复使用。",
        "",
        f"公式能力：{report.formula_engine}；显示能力：{report.display_engine}。",
        "",
        "显示检查为受限 OOXML 格式、尺寸和视图边界验证，不宣称已经由 Excel/WPS 实机渲染。超出能力的强制项记为 not_run。",
        "",
        "## 检查结果",
        "",
        "| 检查 | 结果 | 依据 |",
        "| --- | --- | --- |",
    ]
    lines.extend(f"| {c.name} | {c.status} | {c.detail.replace('|', '/')} |" for c in report.checks)
    lines += [
        "",
        "## 业务量及覆盖",
        "",
        f"A 原始明细：{statistics.a_detail_count} 条。",
        f"B 本月原始行：{statistics.b_current_month_original_rows} 行；按方向拆分的业务记录：{statistics.b_current_month_business_records} 条。",
        f"覆盖等式：{statistics.b_current_month_business_records} = 已承接 {statistics.taken_business_records} + 已标注缺失 {statistics.missing_business_records} + 人工排除 {statistics.excluded_business_records}。",
        f"非本月记录：{statistics.outside_period_business_records} 条；月份待确认：{statistics.unknown_period_business_records} 条。",
        "",
        "| A工作表 | A数量 | C数量 | 数量差额 | A金额 | C金额 | 金额差额 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    lines.extend(
        f"| {t.sheet_name} | {t.a_quantity} | {t.c_quantity} | {t.quantity_difference} | {t.a_amount} | {t.c_amount} | {t.amount_difference} |"
        for t in statistics.totals
    )
    lines += [
        "",
        "## B 记录去向",
        "",
        "| 来源标识 | 去向 | 数量余额 | 金额余额 | 依据 |",
        "| --- | --- | --- | --- | --- |",
    ]
    lines.extend(
        f"| {r.b_record_id} | {r.disposition} | {r.remaining_quantity if r.remaining_quantity is not None else '不可计算'} | {r.remaining_amount if r.remaining_amount is not None else '不可计算'} | {r.explanation.replace('|', '/')} |"
        for r in report.coverage
    )
    lines += ["", "## 系统问题与修复回流", ""]
    lines.extend(
        f"- `{i.code}` → {i.stage}：{i.message}；位置 {i.output_location or '未生成输出位置'}；期望 {i.expected or '见来源'}；实际 {i.actual or '见文件'}。"
        for i in report.issues
    )
    if not report.issues:
        lines.append("没有系统验收问题。业务缺失、排除及待复核事项仍应按单独明细处理。")
    return "\n".join(lines) + "\n"


class DeliveryService:
    """同一任务共用执行锁，只有实际文件全部强制验收通过才注册最终文件。"""

    def __init__(
        self,
        settings: Settings,
        *,
        generator: CGenerator | None = None,
        verifier: FileVerifier | None = None,
    ) -> None:
        """组装保真生成和独立重读验收能力。

        Args:
            settings: 任务目录及工作簿读取资源限制。
            generator: 可注入的 C 生成器，便于故障修复回归。
            verifier: 可注入的实际文件验收器。
        """
        self.repository = LocalTaskRepository(settings.storage_root)
        self.matching = MatchingService(settings)
        self.generator = generator or CGenerator()
        self.verifier = verifier or FileVerifier(
            settings.max_archive_bytes * 3, settings.max_cells * 5
        )

    def _load(self, task_id: str, version: int) -> DeliveryInputs:
        """校验并冻结当前来源、准备数据及正式匹配关系。

        Args:
            task_id: 已完成匹配的任务编号。
            version: 调用方持有的匹配方案版本。

        Returns:
            全部文件摘要及组容量已验证的不可变输入对象。
        """
        plan = self.matching.plan(task_id, expected_version=version)
        if plan.readiness != "ready":
            raise PreparationError(
                "MATCH_PLAN_NEEDS_INPUT", "匹配方案存在阻塞问题，需先完成第二块", http_status=409
            )
        prepared = self.repository.read_model(
            task_id, plan.prepared_data_ref.relative_path, PreparedData
        )
        a = self.repository.read_model(task_id, prepared.a_records_ref.relative_path, RecordSet)
        b = self.repository.read_model(task_id, prepared.b_records_ref.relative_path, RecordSet)
        snapshots = self.repository.read_model(
            task_id, prepared.source_snapshot_ref.relative_path, SourceSnapshots
        )
        matches = self.matching.artifacts(task_id)
        if matches.match_plan_version != version:
            raise PreparationError("STALE_MATCH_PLAN", "匹配版本已变化", http_status=409)
        validate_groups(
            matches.groups,
            a.records,
            b.records,
            plan.rules,
            {r.b_record_id for r in matches.exclusions},
        )
        confirmations = self.repository.read_model(
            task_id, plan.confirmations_ref.relative_path, ConfirmationsArtifact
        )
        active_exclusions: dict[str, tuple[str, str]] = {}
        for confirmation in confirmations.records:
            decision = confirmation.confirmation.decision
            if decision.action == "exclude_b":
                for identifier in decision.b_record_ids:
                    active_exclusions[identifier] = (
                        confirmation.confirmation_id,
                        confirmation.confirmation.reason,
                    )
            elif decision.action == "restore_b":
                for identifier in decision.b_record_ids:
                    active_exclusions.pop(identifier, None)
        if active_exclusions != {
            e.b_record_id: (e.confirmation_ref, e.reason) for e in matches.exclusions
        }:
            raise PreparationError(
                "INVALID_EXCLUSION_CONFIRMATION", "人工排除与实际确认历史不一致，需回到第二块修复"
            )
        expected_groups = {
            identifier: group
            for group in matches.groups
            for identifier in group.candidate.a_record_ids
        }
        for row in matches.rows:
            group = expected_groups.get(row.a_record_id)
            if (
                row.match_group_ids != ((group.group_id,) if group else ())
                or group is not None
                and (
                    row.status != group.status
                    or (group.candidate.relation_type == "partial") != (group.status == "partial")
                )
            ):
                raise PreparationError(
                    "INVALID_MATCH_RESULT", "A 行状态、正式匹配组或部分关系不一致，需修复第二块"
                )
        a_ids = {r.record_id for r in a.records}
        if len(matches.rows) != len(a_ids) or {r.a_record_id for r in matches.rows} != a_ids:
            raise PreparationError("INVALID_A_COVERAGE", "匹配方案缺少唯一的 A 行结果")
        state = self.matching.get(task_id)
        if state.plan_ref is None:
            raise PreparationError("MATCH_PLAN_NOT_READY", "匹配方案尚未发布", http_status=409)
        baseline = self.verifier.read(
            self.repository.path(task_id, prepared.c_baseline_ref.relative_path), FileRole.A
        )
        return DeliveryInputs(
            prepared=prepared,
            plan=plan,
            plan_ref=state.plan_ref,
            matches=matches,
            snapshots=snapshots,
            baseline=baseline,
            baseline_path=self.repository.path(task_id, prepared.c_baseline_ref.relative_path),
            b_source_path=self.repository.path(
                task_id,
                next(
                    w.source_file.relative_path
                    for w in snapshots.workbooks
                    if w.source_file.role == FileRole.B
                ),
            ),
            a_records=a.records,
            b_records=b.records,
        )

    def _save(self, state: DeliveryState) -> DeliveryState:
        """原子发布当前阶段，所有生成版本文件先写后切换指针。

        Args:
            state: 已通过严格类型校验的完整任务头。

        Returns:
            已成功保存的同一状态。
        """
        self.repository.write_model(state.task_id, "delivery/state.json", state)
        return state

    def _event(
        self, state: DeliveryState, stage: str, detail: str, *, attempt: int | None = None
    ) -> DeliveryState:
        """记录真实执行动作，事件时间和次数均来自本次实际操作。

        Args:
            state: 当前生成版本及已有时间线。
            stage: 实际动作阶段。
            detail: 结果、条数或等待原因。
            attempt: 可选的本轮自动修复尝试序号。

        Returns:
            原子保存新动作后的状态。
        """
        event = DeliveryEvent(
            sequence=len(state.events) + 1,
            occurred_at=datetime.now(UTC),
            stage=stage,
            detail=detail,
        )
        return self._save(
            state.model_copy(
                update={
                    "stage": stage,
                    "events": (*state.events, event),
                    "attempt": attempt if attempt is not None else state.attempt,
                }
            )
        )

    def get(self, task_id: str) -> DeliveryState:
        """查询实际阶段，并拒绝把旧匹配版本的产物称作最新结果。

        Args:
            task_id: 已开始第三块的任务。

        Returns:
            与当前匹配方案相容的真实生成状态。
        """
        state = self.repository.read_model(task_id, "delivery/state.json", DeliveryState)
        plan = self.matching.plan(task_id, expected_version=state.match_plan_version)
        if plan.prepared_data_version != state.data_version:
            raise PreparationError(
                "STALE_DELIVERY", "准备数据版本已变化，需重新生成", http_status=409
            )
        return state

    def run(self, task_id: str, request: DeliveryRequest) -> DeliveryState:
        """生成或恢复制表任务，已完成同一输入重跑幂等返回。

        Args:
            task_id: 第一、第二块共用的任务编号。
            request: 预期匹配版本、执行能力及是否明确重新生成。

        Returns:
            完成、带业务复核完成、需要输入或失败的真实状态。
        """
        try:
            with self.repository.lock(task_id):
                inputs = self._load(task_id, request.expected_match_plan_version)
                previous = (
                    self.repository.read_model(task_id, "delivery/state.json", DeliveryState)
                    if self.repository.path(task_id, "delivery/state.json").is_file()
                    else None
                )
                compatible = (
                    previous is not None
                    and previous.plan_ref == inputs.plan_ref
                    and previous.options == request.options
                )
                if (
                    previous is not None
                    and compatible
                    and previous.status in FINAL_STATUSES
                    and not request.force
                ):
                    try:
                        self.result(task_id, previous.generation_version)
                        return previous
                    except PreparationError:
                        logger.info(
                            "Published artifact changed; rebuilding a new delivery generation"
                        )
                if (
                    previous is not None
                    and compatible
                    and previous.status == "running"
                    and not request.force
                ):
                    state = previous
                else:
                    state = DeliveryState(
                        task_id=task_id,
                        data_version=inputs.plan.prepared_data_version,
                        match_plan_version=inputs.plan.match_plan_version,
                        generation_version=previous.generation_version + 1 if previous else 1,
                        status="running",
                        stage="freeze_inputs",
                        options=request.options,
                        plan_ref=inputs.plan_ref,
                    )
                state = self._event(
                    state, "freeze_inputs", "已验证源文件、准备数据、匹配组、分配及全部引用摘要"
                )
                return self._execute(state, inputs)
        except Timeout as exc:
            raise PreparationError(
                "TASK_BUSY", "同一任务正在执行，请稍后查询实际进度", http_status=409
            ) from exc

    def _execute(self, state: DeliveryState, inputs: DeliveryInputs) -> DeliveryState:
        """执行受限自动修复，每轮均从底稿重新生成并重读实际产物。

        Args:
            state: 冻结输入后的当前生成版本。
            inputs: 经过摘要和组关系校验的来源。

        Returns:
            原子注册最终结果或保留诊断产物的状态。
        """
        baseline_path = self.repository.path(
            state.task_id, inputs.prepared.c_baseline_ref.relative_path
        )
        b_source = next(
            w.source_file for w in inputs.snapshots.workbooks if w.source_file.role == FileRole.B
        )
        for attempt in range(1, state.options.max_auto_repairs + 2):
            base = f"delivery/versions/{state.generation_version}/attempts/{attempt}"
            diagnostics: list[ArtifactRef] = []
            try:
                state = self._event(
                    state,
                    "generate_c",
                    "从完整底稿生成 C，展开正式 B 来源并写入独立分配",
                    attempt=attempt,
                )
                c_content, manifest = self.generator.generate(
                    baseline_path, inputs, state.generation_version
                )
                c_ref = self.repository.write_bytes(
                    state.task_id, f"{base}/c_reconciled.xlsx", c_content
                )
                diagnostics.append(c_ref)
                manifest_ref = self.repository.write_model(
                    state.task_id, f"{base}/manifest.json", manifest
                )
                diagnostics.append(manifest_ref)
                c_path = self.repository.path(state.task_id, c_ref.relative_path)
                state = self._event(
                    state,
                    "inspect_actual_c",
                    "重新打开实际 C，逐条核对 B 原始字段及独立来源出现次数",
                )
                inspection = self.verifier.inspect_c(c_path, inputs, manifest)
                coverage = self.verifier.coverage(inputs, inspection)
                state = self._event(
                    state, "mark_b", "按实际 C 事实生成 B 副本的本月缺失、部分余额和人工排除标注"
                )
                b_content = mark_b(
                    self.repository.path(state.task_id, b_source.relative_path), inputs, coverage
                )
                b_ref = self.repository.write_bytes(
                    state.task_id, f"{base}/b_annotated.xlsx", b_content
                )
                diagnostics.append(b_ref)
                state = self._event(
                    state,
                    "verify_files",
                    "独立重读 C 和 B，核查 A/C 合计、B 覆盖、精度、公式及显示边界",
                )
                report = self.verifier.verify(
                    c_path,
                    self.repository.path(state.task_id, b_ref.relative_path),
                    inputs,
                    manifest,
                    state.options,
                )
                report_ref = self.repository.write_model(
                    state.task_id, f"{base}/verification_report.json", report
                )
                report_text_ref = self.repository.write_bytes(
                    state.task_id,
                    f"{base}/verification_report.md",
                    readable_report(report).encode("utf-8"),
                )
                reviews = DeliveryReviewItems(
                    matching=inputs.matches.reviews,
                    b_business_items=tuple(
                        r for r in coverage if r.disposition in {"missing", "unknown_period"}
                    ),
                )
                review_ref = self.repository.write_model(
                    state.task_id, f"{base}/review_items.json", reviews
                )
                diagnostics += [report_ref, report_text_ref, review_ref]
                state = self._save(
                    state.model_copy(
                        update={"issues": report.issues, "diagnostic_refs": tuple(diagnostics)}
                    )
                )
                if (
                    not report.passed
                    and report.issues
                    and all(i.repairable for i in report.issues)
                    and attempt <= state.options.max_auto_repairs
                ):
                    state = self._event(
                        state,
                        "repair_delivery",
                        "验收失败回流第三块，按同一冻结版本重新生成全部受影响文件",
                    )
                    continue
                return self._finish(
                    state,
                    inputs,
                    report,
                    c_ref,
                    b_ref,
                    manifest_ref,
                    report_ref,
                    report_text_ref,
                    review_ref,
                    bool(reviews.matching or reviews.b_business_items),
                )
            except Exception as exc:
                logger.exception("Delivery generation failed: %s", state.task_id)
                problem = issue(
                    exc.code if isinstance(exc, PreparationError) else "DELIVERY_TOOL_FAILED",
                    exc.message
                    if isinstance(exc, PreparationError)
                    else "制表或核查工具执行失败，已保留执行位置及诊断文件",
                )
                state = self._save(
                    state.model_copy(
                        update={"issues": (problem,), "diagnostic_refs": tuple(diagnostics)}
                    )
                )
                if attempt <= state.options.max_auto_repairs:
                    state = self._event(
                        state, "repair_delivery", "工具失败，使用剩余修复预算重试，验收标准保持不变"
                    )
                    continue
                state = self._event(state, "failed", problem.message)
                state = state.model_copy(update={"status": "failed", "result_ref": None})
                diagnostic = self.repository.write_model(
                    state.task_id, f"{base}/execution_diagnostic.json", state
                )
                return self._save(
                    state.model_copy(
                        update={"diagnostic_refs": (*state.diagnostic_refs, diagnostic)}
                    )
                )
        raise RuntimeError("自动修复循环未产生明确结果")

    def _finish(
        self,
        state: DeliveryState,
        inputs: DeliveryInputs,
        report: VerificationReport,
        c_ref: ArtifactRef,
        b_ref: ArtifactRef,
        manifest_ref: ArtifactRef,
        report_ref: ArtifactRef,
        report_text_ref: ArtifactRef,
        review_ref: ArtifactRef,
        has_review: bool,
    ) -> DeliveryState:
        """核查通过后才原子注册正式交付，失败文件仅注册为诊断。

        Args:
            state: 当前生成任务及真实执行记录。
            inputs: 本次冻结的来源。
            report: 对实际文件的完整验收报告。
            c_ref: 实际 C 文件摘要引用。
            b_ref: 实际标注 B 文件摘要引用。
            manifest_ref: 当前生成布局及版本说明。
            report_ref: 结构化核查报告。
            report_text_ref: 可阅读核查报告。
            review_ref: 独立业务复核明细。
            has_review: 是否仍有真实业务待复核事项。

        Returns:
            指向当前版本结果对象的正式状态。
        """
        # 发布前再次验证当前匹配及源文件，拒绝把旧方案注册成最新交付。
        current = self.matching.plan(state.task_id, expected_version=state.match_plan_version)
        if current != inputs.plan:
            raise PreparationError("STALE_MATCH_PLAN", "生成期间输入版本发生变化", http_status=409)
        status: Literal["completed", "completed_with_review", "needs_input", "failed"]
        verified = (
            report.passed
            and not report.issues
            and bool(report.checks)
            and all(c.status == "pass" for c in report.checks if c.mandatory)
        )
        if verified:
            status = "completed_with_review" if has_review else "completed"
        else:
            status = (
                "needs_input"
                if any(i.stage in {"input", "preparation"} for i in report.issues)
                or any(c.status == "not_run" for c in report.checks)
                else "failed"
            )
        pairs: tuple[
            tuple[
                Literal[
                    "c_workbook",
                    "annotated_b",
                    "verification_json",
                    "verification_readable",
                    "reviews",
                    "manifest",
                ],
                ArtifactRef,
            ],
            ...,
        ] = (
            ("c_workbook", c_ref),
            ("annotated_b", b_ref),
            ("verification_json", report_ref),
            ("verification_readable", report_text_ref),
            ("reviews", review_ref),
            ("manifest", manifest_ref),
        )
        artifacts = tuple(
            DeliveryArtifact(
                kind=kind,
                reference=reference,
                data_version=state.data_version,
                match_plan_version=state.match_plan_version,
                generation_version=state.generation_version,
            )
            for kind, reference in pairs
        )
        result = DeliveryResult(
            task_id=state.task_id,
            data_version=state.data_version,
            match_plan_version=state.match_plan_version,
            generation_version=state.generation_version,
            status=status,
            artifacts=artifacts,
            statistics=report.statistics,
            verification_report_ref=report_ref,
            review_items_ref=review_ref,
        )
        result_ref = self.repository.write_model(
            state.task_id, f"delivery/versions/{state.generation_version}/result.json", result
        )
        state = self._event(
            state,
            status,
            "强制验收全部通过，已注册当前版本交付文件"
            if verified
            else "强制验收未通过，文件仅作为诊断，需按报告定位修复",
        )
        return self._save(
            state.model_copy(
                update={"status": status, "result_ref": result_ref, "issues": report.issues}
            )
        )

    def _verify_ref(self, task_id: str, reference: ArtifactRef) -> None:
        """下载或读取前校验实际文件，避免登记后被改写仍显示完成。

        Args:
            task_id: 文件所属任务。
            reference: 已登记的内容摘要引用。
        """
        path = self.repository.path(task_id, reference.relative_path)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != reference.sha256:
            raise PreparationError(
                "OUTPUT_INTEGRITY_ERROR",
                "交付文件缺失或摘要不一致，请重新验收或生成",
                http_status=409,
            )

    def result(self, task_id: str, expected_generation_version: int) -> DeliveryResult:
        """读取当前版本结果，失败结果不冒充正式交付。

        Args:
            task_id: 制表任务编号。
            expected_generation_version: 调用方持有的生成版本。

        Returns:
            全部文件摘要一致的结果和真实状态。
        """
        state = self.get(task_id)
        if state.generation_version != expected_generation_version:
            raise PreparationError("STALE_DELIVERY", "生成版本已变化", http_status=409)
        if state.result_ref is None:
            raise PreparationError("DELIVERY_NOT_READY", "交付结果尚未生成", http_status=409)
        self._verify_ref(task_id, state.result_ref)
        result = self.repository.read_model(task_id, state.result_ref.relative_path, DeliveryResult)
        if (
            result.status != state.status
            or result.match_plan_version != state.match_plan_version
            or result.generation_version != state.generation_version
        ):
            raise PreparationError(
                "STALE_DELIVERY", "结果与当前任务状态或版本不一致", http_status=409
            )
        for artifact in result.artifacts:
            self._verify_ref(task_id, artifact.reference)
        return result

    def artifact(
        self, task_id: str, name: str, expected_generation_version: int, *, diagnostic: bool = False
    ) -> Path:
        """按版本和白名单返回正式文件或明确标记的诊断文件。

        Args:
            task_id: 制表任务编号。
            name: 当前清单中实际存在的文件名。
            expected_generation_version: 当前生成版本。
            diagnostic: True 允许读取诊断清单；不影响正式完成门槛。

        Returns:
            校验通过的绝对文件路径，旧匹配或旧生成版本均返回冲突。
        """
        state = self.get(task_id)
        if expected_generation_version != state.generation_version:
            raise PreparationError("STALE_DELIVERY", "下载链接的生成版本已过期", http_status=409)
        if diagnostic:
            refs = state.diagnostic_refs
        else:
            if state.status not in FINAL_STATUSES:
                raise PreparationError(
                    "DELIVERY_NOT_VERIFIED",
                    "强制验收未通过，只能下载明确标记的诊断文件",
                    http_status=409,
                )
            refs = tuple(
                a.reference for a in self.result(task_id, expected_generation_version).artifacts
            )
        reference = next((r for r in refs if r.name == name), None)
        if reference is None:
            raise PreparationError("ARTIFACT_NOT_FOUND", "交付文件不存在", http_status=404)
        self._verify_ref(task_id, reference)
        return self.repository.path(task_id, reference.relative_path)

    def report(self, task_id: str, expected_generation_version: int) -> VerificationReport:
        """读取当前正式或诊断验收报告，页面据此展示真实检查结论。

        Args:
            task_id: 制表任务编号。
            expected_generation_version: 页面持有的当前生成版本。

        Returns:
            当前版本的实际文件验收报告，尚未验收时返回冲突。
        """
        state = self.get(task_id)
        if state.generation_version != expected_generation_version:
            raise PreparationError("STALE_DELIVERY", "生成版本已变化", http_status=409)
        reference = next(
            (r for r in state.diagnostic_refs if r.name == "verification_report.json"), None
        )
        if reference is None:
            raise PreparationError(
                "VERIFICATION_NOT_READY", "实际文件验收尚未形成报告", http_status=409
            )
        self._verify_ref(task_id, reference)
        return self.repository.read_model(task_id, reference.relative_path, VerificationReport)

    def recheck(self, task_id: str, expected_generation_version: int) -> DeliveryState:
        """对已生成文件再次独立验收，发现修改立即撤销当前正式下载资格。

        Args:
            task_id: 已生成的制表任务。
            expected_generation_version: 需要复验的当前生成版本。

        Returns:
            复验仍通过的状态，或保留新诊断报告的 failed/needs_input 状态。
        """
        try:
            with self.repository.lock(task_id):
                state = self.get(task_id)
                if (
                    state.generation_version != expected_generation_version
                    or state.result_ref is None
                ):
                    raise PreparationError(
                        "STALE_DELIVERY", "生成版本过期或尚未生成结果", http_status=409
                    )
                try:
                    result, report = self._inspect_registered(state)
                except (OSError, ValueError, PreparationError, KeyError) as exc:
                    problem = issue(
                        "OUTPUT_UNREADABLE",
                        "登记后的实际文件缺失、损坏或无法重新验收，已撤销正式交付资格",
                        actual=str(exc),
                    )
                    state = self._event(state, "failed", problem.message)
                    failed = state.model_copy(
                        update={"status": "failed", "issues": (problem,), "result_ref": None}
                    )
                    ref = self.repository.write_model(
                        task_id,
                        f"delivery/versions/{state.generation_version}/recheck_{len(state.events)}/execution_diagnostic.json",
                        failed,
                    )
                    remaining = tuple(
                        self.repository.reference(task_id, r.relative_path)
                        for r in state.diagnostic_refs
                        if r.name in {"c_reconciled.xlsx", "b_annotated.xlsx", "manifest.json"}
                        and self.repository.path(task_id, r.relative_path).is_file()
                    )
                    return self._save(
                        failed.model_copy(update={"diagnostic_refs": (ref, *remaining)})
                    )
                changed = self._changed_outputs(task_id, result.artifacts)
                if state.result_ref is not None:
                    actual_result_hash = self.repository.reference(
                        task_id, state.result_ref.relative_path
                    ).sha256
                    if actual_result_hash != state.result_ref.sha256:
                        changed += (
                            issue(
                                "OUTPUT_INTEGRITY_ERROR",
                                "登记结果对象的内容摘要发生变化",
                                location=state.result_ref.relative_path,
                                expected=state.result_ref.sha256,
                                actual=actual_result_hash,
                            ),
                        )
                if report.passed and not changed:
                    return self._event(
                        state, state.status, "对实际交付文件重新验收通过，摘要及所有强制项保持一致"
                    )
                report = report.model_copy(
                    update={
                        "passed": False,
                        "issues": (*report.issues, *changed),
                        "checks": (
                            *report.checks,
                            VerificationCheck(
                                name="artifact_integrity",
                                status="fail" if changed else "pass",
                                detail="重新核对全部登记文件的实际 SHA-256",
                            ),
                        ),
                    }
                )
                base = (
                    f"delivery/versions/{state.generation_version}/recheck_{len(state.events) + 1}"
                )
                ref = self.repository.write_model(
                    task_id, f"{base}/verification_report.json", report
                )
                text_ref = self.repository.write_bytes(
                    task_id,
                    f"{base}/verification_report.md",
                    readable_report(report).encode("utf-8"),
                )
                state = self._event(
                    state, "failed", "实际文件复验未通过，撤销当前正式交付资格；需修复或重新生成"
                )
                workbook_refs = tuple(
                    self.repository.reference(task_id, a.reference.relative_path)
                    for a in result.artifacts
                    if a.kind in {"c_workbook", "annotated_b", "manifest"}
                    and self.repository.path(task_id, a.reference.relative_path).is_file()
                )
                return self._save(
                    state.model_copy(
                        update={
                            "status": "failed",
                            "issues": report.issues,
                            "result_ref": None,
                            "diagnostic_refs": (ref, text_ref, *workbook_refs),
                        }
                    )
                )
        except Timeout as exc:
            raise PreparationError("TASK_BUSY", "同一任务正在执行", http_status=409) from exc

    def _inspect_registered(
        self, state: DeliveryState
    ) -> tuple[DeliveryResult, VerificationReport]:
        """重新打开已登记文件进行验收，解析错误由调用方撤销交付资格。

        Args:
            state: 持有当前结果引用的生成状态。

        Returns:
            登记结果与实际文件重新验收的报告。
        """
        if state.result_ref is None:
            raise PreparationError("DELIVERY_NOT_READY", "没有可复验的结果")
        result = self.repository.read_model(
            state.task_id, state.result_ref.relative_path, DeliveryResult
        )
        artifacts = {a.kind: a.reference for a in result.artifacts}
        inputs = self._load(state.task_id, state.match_plan_version)
        manifest = self.repository.read_model(
            state.task_id, artifacts["manifest"].relative_path, GenerationManifest
        )
        report = self.verifier.verify(
            self.repository.path(state.task_id, artifacts["c_workbook"].relative_path),
            self.repository.path(state.task_id, artifacts["annotated_b"].relative_path),
            inputs,
            manifest,
            state.options,
        )
        return result, report

    def _changed_outputs(
        self, task_id: str, artifacts: tuple[DeliveryArtifact, ...]
    ) -> tuple[VerificationIssue, ...]:
        """将缺失或摘要变化的文件逐一记录，不因文件缺失而中断撤销流程。

        Args:
            task_id: 文件所属任务。
            artifacts: 原正式登记的产物和期望摘要。

        Returns:
            全部发生变化或已经丢失的产物问题。
        """
        changed: list[VerificationIssue] = []
        for artifact in artifacts:
            ref = artifact.reference
            actual_hash = (
                self.repository.reference(task_id, ref.relative_path).sha256
                if self.repository.path(task_id, ref.relative_path).is_file()
                else "missing"
            )
            if actual_hash != ref.sha256:
                changed.append(
                    issue(
                        "OUTPUT_INTEGRITY_ERROR",
                        "复验发现登记后的文件内容变化或缺失",
                        location=ref.relative_path,
                        expected=ref.sha256,
                        actual=actual_hash,
                    )
                )
        return tuple(changed)
