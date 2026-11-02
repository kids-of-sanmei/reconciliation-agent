"""第一块阶段用例；LangGraph 节点调用这些用例而不承载文件细节。"""

import hashlib
from datetime import UTC, datetime

from app.preparation.application.mapping import build_profiles, detect_mappings, validate_mappings
from app.preparation.application.normalization import RecordNormalizer, build_index, parse_decimal
from app.preparation.application.period import infer_reconciliation_period
from app.preparation.application.tasks import transition
from app.preparation.domain.errors import PreparationError
from app.preparation.domain.models import (
    Confirmation,
    ConfirmationRecord,
    FileRole,
    Issue,
    PreparedData,
    RecordSet,
    SheetMapping,
    SourceSnapshots,
    TaskContext,
    TaskOptions,
    TaskStatus,
    WorkbookSnapshot,
)
from app.preparation.infrastructure.storage import LocalTaskRepository
from app.preparation.ports import MappingAdvisor, WorkbookGateway


class PreparationSteps:
    """数据准备阶段的可测试用例集合。"""

    def __init__(
        self,
        repository: LocalTaskRepository,
        workbook: WorkbookGateway,
        advisor: MappingAdvisor | None = None,
    ) -> None:
        """注入基础设施。

        Args:
            repository: 任务和版本产物仓库。
            workbook: 可替换的工作簿格式适配器。
            advisor: 可选的模型结构识别器，未配置时使用模板及人工映射。
        """
        self.repository = repository
        self.workbook = workbook
        self.advisor = advisor
        self.normalizer = RecordNormalizer()

    def parse(self, task_id: str) -> None:
        """解析不可变输入并保存快照及摘要。

        Args:
            task_id: 要执行的任务标识。
        """
        task = self.repository.get_task(task_id)
        self.repository.save_task(transition(task, TaskStatus.PARSING, "parse_workbooks"))
        workbooks: list[WorkbookSnapshot] = []
        for source in task.sources:
            self.repository.record_event(
                task_id, "parse_workbooks", "started", f"开始读取 {source.original_name}"
            )
            workbook = self.workbook.read(
                self.repository.path(task_id, source.relative_path), source
            )
            workbooks.append(workbook)
            self.repository.record_event(
                task_id,
                "parse_workbooks",
                "completed",
                f"已解析 {workbook.source_file.original_name}",
                f"读取 {len(workbook.sheets)} 张工作表，"
                f"保存 {sum(len(sheet.cells) for sheet in workbook.sheets)} 个原始单元格。",
            )
        snapshots = SourceSnapshots(workbooks=tuple(workbooks))
        self.repository.write_model(task_id, "sources/snapshot.json", snapshots)
        self.repository.write_model(task_id, "sources/profiles.json", build_profiles(snapshots))

    def identify(self, task_id: str) -> None:
        """组合已确认映射、确定性表头规则及可选模型建议。

        Args:
            task_id: 已完成来源解析的任务。
        """
        task = self.repository.get_task(task_id)
        self.repository.save_task(transition(task, TaskStatus.IDENTIFYING, "identify_fields"))
        snapshots = self.repository.read_model(task_id, "sources/snapshot.json", SourceSnapshots)
        mappings = list(task.options.mappings)
        missing_roles = {
            role
            for role in (FileRole.A, FileRole.B)
            if not any(item.role == role for item in mappings)
        }
        for workbook in snapshots.workbooks:
            if workbook.source_file.role in missing_roles:
                candidates = detect_mappings(workbook)
                if len(candidates) == 1:
                    mappings.extend(candidates)
                    self.repository.record_event(
                        task_id,
                        "identify_fields",
                        "completed",
                        f"规则识别 {workbook.source_file.role.value.upper()} 表字段成功",
                        f"工作表：{candidates[0].sheet_name}；表头第 {candidates[0].header_row} 行。",
                    )
        issues: list[Issue] = []
        if self.advisor is not None and any(
            not any(item.role == role for item in mappings) for role in (FileRole.A, FileRole.B)
        ):
            try:
                unresolved_roles = {
                    role
                    for role in (FileRole.A, FileRole.B)
                    if not any(item.role == role for item in mappings)
                }
                main_snapshots = SourceSnapshots(
                    workbooks=tuple(
                        item
                        for item in snapshots.workbooks
                        if item.source_file.role in unresolved_roles
                    )
                )
                self.repository.record_event(
                    task_id,
                    "identify_fields",
                    "started",
                    "调用模型辅助识别字段",
                    "规则尚未确定全部字段，向模型提交表结构摘要及有限样例。",
                )
                advice = self.advisor.suggest(build_profiles(main_snapshots))
                self.repository.write_model(
                    task_id, f"versions/{task.data_version}/mapping_advice.json", advice
                )
                for role in (FileRole.A, FileRole.B):
                    if not any(item.role == role for item in mappings):
                        mappings.extend(item for item in advice.mappings if item.role == role)
                self.repository.record_event(
                    task_id,
                    "identify_fields",
                    "completed",
                    "模型已返回字段建议",
                    f"返回 {len(advice.mappings)} 项映射建议，继续执行程序校验。",
                )
            except PreparationError as exc:
                if exc.code == "PROMPT_CONFIGURATION_ERROR":
                    raise
                # 模型服务故障可通过人工映射恢复，不泄露供应商错误中的密钥或请求内容。
                issues.append(
                    Issue(
                        code="MODEL_MAPPING_UNAVAILABLE",
                        message="模型识别暂不可用，请提供明确字段映射或稍后重试",
                        blocking=False,
                    )
                )
                self.repository.record_event(
                    task_id,
                    "identify_fields",
                    "info",
                    "模型识别暂不可用",
                    "可提供人工字段映射后继续执行，或稍后重试。",
                )
        else:
            self.repository.record_event(
                task_id,
                "identify_fields",
                "info",
                "本次字段识别未调用模型",
                "已确认映射及规则已覆盖 A/B 表。"
                if all(
                    any(item.role == role for item in mappings) for role in (FileRole.A, FileRole.B)
                )
                else "未配置模型，无法确定的字段需要人工确认。",
            )
        resolved_mappings = tuple(mappings)
        mapping_issues = validate_mappings(snapshots, resolved_mappings)
        period = task.options.reconciliation_period
        if period is None and not mapping_issues:
            period = infer_reconciliation_period(snapshots, resolved_mappings)
            if period is not None:
                self.repository.record_event(
                    task_id,
                    "identify_fields",
                    "completed",
                    "已自动识别对账月份",
                    f"根据 A 表明细日期确定为 {period}，未使用当前时间或文件名猜测年份。",
                )
        options = TaskOptions(
            reconciliation_period=period,
            mappings=resolved_mappings,
            specification_aliases=task.options.specification_aliases,
            date_tolerance_days=task.options.date_tolerance_days,
        )
        issues.extend(mapping_issues)
        if options.reconciliation_period is None:
            issues.append(
                Issue(code="PERIOD_REQUIRED", message="请提供明确的对账年月，例如 2026-07")
            )
        status = (
            TaskStatus.NEEDS_INPUT
            if any(issue.blocking for issue in issues)
            else TaskStatus.PREPARING
        )
        self.repository.save_task(
            transition(task, status, "identify_fields", issues=tuple(issues), options=options)
        )

    def validate_confirmation(self, task_id: str, confirmation: Confirmation) -> None:
        """在消费检查点之前检查补充数据是否合法。

        Args:
            task_id: 正等待输入的任务。
            confirmation: 用户提交的完整配置或公式确认结果。

        Raises:
            PreparationError: 映射无效、公式来源不存在或数值非法。
        """
        snapshots = self.repository.read_model(task_id, "sources/snapshot.json", SourceSnapshots)
        if confirmation.options is not None and confirmation.options.mappings:
            issues = validate_mappings(snapshots, confirmation.options.mappings)
            if issues:
                raise PreparationError(
                    "INVALID_MAPPING", "; ".join(issue.message for issue in issues)
                )
        formula_refs = {
            f"{workbook.source_file.file_id}:{sheet.sheet_id}:{cell.coordinate}"
            for workbook in snapshots.workbooks
            for sheet in workbook.sheets
            for cell in sheet.cells.values()
            if cell.kind == "formula"
        }
        for reference, value in confirmation.formula_values.items():
            if reference not in formula_refs:
                raise PreparationError("UNKNOWN_FORMULA", "公式补充值必须指向真实的来源公式单元格")
            try:
                parse_decimal(value)
            except ValueError as exc:
                raise PreparationError(
                    "INVALID_FORMULA_VALUE", "公式补充值必须为有限十进制数字"
                ) from exc

    def confirm(self, task_id: str, confirmation: Confirmation) -> None:
        """保存审计并增加版本，下一节点重新识别受影响数据。

        Args:
            task_id: 等待补充的任务。
            confirmation: 已通过业务校验的人员确认。
        """
        self.validate_confirmation(task_id, confirmation)
        task = self.repository.get_task(task_id)
        if task.confirmations and task.confirmations[-1].confirmation == confirmation:
            return
        self.repository.save_task(
            transition(
                task,
                TaskStatus.IDENTIFYING,
                "apply_confirmation",
                options=confirmation.options if confirmation.options is not None else task.options,
                formula_values=task.formula_values | confirmation.formula_values,
                confirmations=task.confirmations
                + (
                    ConfirmationRecord(
                        confirmation=confirmation,
                        confirmed_at=datetime.now(UTC),
                        input_data_version=task.data_version,
                        output_data_version=task.data_version + 1,
                    ),
                ),
                data_version=task.data_version + 1,
            )
        )

    def prepare(self, task_id: str) -> None:
        """标准化明细，生成底稿、索引、合计及下游数据包。

        Args:
            task_id: 字段和期间已明确的任务。
        """
        task = self.repository.get_task(task_id)
        self.repository.save_task(transition(task, TaskStatus.PREPARING, "normalize_records"))
        for source in task.sources:
            content = self.repository.path(task_id, source.relative_path).read_bytes()
            if hashlib.sha256(content).hexdigest() != source.sha256:
                raise PreparationError("SOURCE_CHANGED", "来源文件在解析后发生变化，请重新创建任务")
        snapshots = self.repository.read_model(task_id, "sources/snapshot.json", SourceSnapshots)
        result = self.normalizer.normalize(snapshots, task.options, task.formula_values)
        version = f"versions/{task.data_version}"
        a_ref = self.repository.write_model(
            task_id, f"{version}/a_records.json", RecordSet(records=result.a_records)
        )
        b_ref = self.repository.write_model(
            task_id, f"{version}/b_records.json", RecordSet(records=result.b_records)
        )
        if any(issue.blocking for issue in result.issues):
            self.repository.save_task(
                transition(task, TaskStatus.NEEDS_INPUT, "normalize_records", issues=result.issues)
            )
            return
        self.repository.record_event(
            task_id,
            "normalize_records",
            "completed",
            "A/B 明细标准化完成",
            f"A 表 {len(result.a_records)} 条，B 表 {len(result.b_records)} 条；"
            f"保留来源位置及原始数值，记录 {len(result.issues)} 项提示。",
        )
        period = task.options.reconciliation_period
        if period is None:
            raise PreparationError("PERIOD_REQUIRED", "缺少对账年月")
        a_snapshot = next(
            item for item in snapshots.workbooks if item.source_file.role == FileRole.A
        )
        a_mappings: tuple[SheetMapping, ...] = tuple(
            item for item in task.options.mappings if item.role == FileRole.A
        )
        self.repository.save_task(transition(task, TaskStatus.PREPARING, "build_baseline"))
        content, audit = self.workbook.create_baseline(
            self.repository.path(task_id, a_snapshot.source_file.relative_path),
            a_snapshot,
            a_mappings,
            result.a_records,
            period,
            task.formula_values,
        )
        baseline_ref = self.repository.write_bytes(task_id, f"{version}/c_baseline.xlsx", content)
        self.repository.write_model(task_id, f"{version}/audit.json", audit)
        self.repository.record_event(
            task_id,
            "build_baseline",
            "completed",
            "C 初始底稿及清理审计已保存",
            "完整复制 A 工作簿，并按确认范围处理合并、底色及数值显示。",
        )
        self.repository.save_task(transition(task, TaskStatus.PREPARING, "build_b_index"))
        index_ref = self.repository.write_model(
            task_id, f"{version}/b_index.json", build_index(result.b_records)
        )
        self.repository.record_event(
            task_id,
            "build_b_index",
            "completed",
            "B 查询索引已保存",
            f"索引引用 {len(result.b_records)} 条标准化记录，供后续匹配使用。",
        )
        self.repository.save_task(transition(task, TaskStatus.PREPARING, "publish_prepared"))
        prepared = PreparedData(
            task_context=TaskContext(
                task_id=task_id,
                reconciliation_period=period,
                files=task.sources,
                field_mappings=task.options.mappings,
                rules=task.options,
                data_version=task.data_version,
                confirmations=task.confirmations,
            ),
            source_snapshot_ref=self.repository.reference(task_id, "sources/snapshot.json"),
            table_profiles_ref=self.repository.reference(task_id, "sources/profiles.json"),
            a_records_ref=a_ref,
            b_records_ref=b_ref,
            b_index_ref=index_ref,
            c_baseline_ref=baseline_ref,
            a_baseline_totals=result.totals,
            preparation_issues=result.issues,
        )
        prepared_ref = f"{version}/prepared.json"
        self.repository.write_model(task_id, prepared_ref, prepared)
        self.repository.save_task(
            transition(
                task,
                TaskStatus.READY,
                "preparation_ready",
                issues=result.issues,
                prepared_ref=prepared_ref,
                artifact_names=(
                    "prepared.json",
                    "a_records.json",
                    "b_records.json",
                    "b_index.json",
                    "c_baseline.xlsx",
                    "audit.json",
                ),
            )
        )
