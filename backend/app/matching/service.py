"""第二块应用服务：输入摘要校验、任务锁、行检查点和原子版本发布。"""

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from filelock import Timeout

from app.core.config import Settings
from app.matching.advisor import MatchingAdvisor, OllamaMatchingAdvisor, StructuredMatchingAdvisor
from app.matching.engine import manual_candidates, reconcile
from app.matching.models import (
    ConfirmationsArtifact,
    Exclusion,
    ExclusionsArtifact,
    GroupsArtifact,
    IssuesArtifact,
    MatchArtifacts,
    MatchConfirmation,
    MatchConfirmationRecord,
    MatchingRules,
    MatchingState,
    MatchPlan,
    ProgressArtifact,
    ReviewsArtifact,
    RowProgress,
    RowsArtifact,
    UsageArtifact,
)
from app.matching.search import canonical
from app.matching.workflow import RowState, RowWorkflow
from app.preparation.application.normalization import build_index
from app.preparation.domain.errors import PreparationError
from app.preparation.domain.models import (
    ArtifactRef,
    BusinessRecord,
    DomainModel,
    Issue,
    LookupIndex,
    PreparedData,
    RecordSet,
    TaskStatus,
)
from app.preparation.infrastructure.storage import LocalTaskRepository

logger = logging.getLogger(__name__)


class MatchingService:
    """只消费第一块正式数据包，不修改 A/B 原始文件和 C 底稿。"""

    def __init__(self, settings: Settings, *, advisor: MatchingAdvisor | None = None) -> None:
        """组装第二块服务和可替换模型端口。

        Args:
            settings: 共用任务存储和模型连接设置。
            advisor: 可注入语义复核器，未提供时按环境决定是否启用。
        """
        self.repository = LocalTaskRepository(settings.storage_root)
        if advisor is None and settings.llm_model:
            if settings.llm_provider == "ollama":
                advisor = OllamaMatchingAdvisor(settings)
            elif settings.llm_api_key is not None and settings.llm_api_key.get_secret_value():
                advisor = StructuredMatchingAdvisor(settings)
        self.workflow = RowWorkflow(advisor).build()

    def _verify(self, task_id: str, reference: ArtifactRef) -> None:
        """验证文件存在和内容摘要，拒绝篡改后的来源。

        Args:
            task_id: 引用所属任务。
            reference: 第一块或本块发布的带摘要引用。
        """
        path = self.repository.path(task_id, reference.relative_path)
        if (
            not path.is_file()
            or self.repository.reference(task_id, reference.relative_path).sha256
            != reference.sha256
        ):
            raise PreparationError(
                "ARTIFACT_INTEGRITY_ERROR", f"文件摘要不一致：{reference.name}", http_status=409
            )

    def _inputs(
        self, task_id: str
    ) -> tuple[PreparedData, ArtifactRef, RecordSet, RecordSet, LookupIndex]:
        """验证当前 PreparedData、全部交接引用和可重建的 B 索引。

        Args:
            task_id: 已完成第一块的任务编号。

        Returns:
            数据包、数据包引用、A/B 集合及已校验索引。
        """
        task = self.repository.get_task(task_id)
        if task.status != TaskStatus.READY or task.prepared_ref is None:
            raise PreparationError("NOT_PREPARED", "第一块尚未准备完成", http_status=409)
        if not self.repository.path(task_id, task.prepared_ref).is_file():
            raise PreparationError(
                "ARTIFACT_INTEGRITY_ERROR", "准备数据包文件缺失", http_status=409
            )
        ref = self.repository.reference(task_id, task.prepared_ref)
        prepared = self.repository.read_model(task_id, task.prepared_ref, PreparedData)
        context = prepared.task_context
        if (
            context.task_id != task.task_id
            or context.data_version != task.data_version
            or context.rules != task.options
            or not context.field_mappings
            or context.rule_version != "preparation-v1"
        ):
            raise PreparationError(
                "INVALID_PREPARED_VERSION", "数据包任务、映射或规则版本不一致", http_status=409
            )
        for reference in (
            prepared.source_snapshot_ref,
            prepared.table_profiles_ref,
            prepared.a_records_ref,
            prepared.b_records_ref,
            prepared.b_index_ref,
            prepared.c_baseline_ref,
        ):
            self._verify(task_id, reference)
        for source in context.files:
            self._verify(
                task_id,
                ArtifactRef(
                    name=source.original_name,
                    relative_path=source.relative_path,
                    sha256=source.sha256,
                ),
            )
        a = self.repository.read_model(task_id, prepared.a_records_ref.relative_path, RecordSet)
        b = self.repository.read_model(task_id, prepared.b_records_ref.relative_path, RecordSet)
        index = self.repository.read_model(task_id, prepared.b_index_ref.relative_path, LookupIndex)
        for records in (a.records, b.records):
            if len({r.record_id for r in records}) != len(records):
                raise PreparationError("DUPLICATE_SOURCE_ID", "原始业务记录标识重复")
        if index != build_index(b.records):
            raise PreparationError("INVALID_B_INDEX", "B 索引与原始业务记录不一致")
        return prepared, ref, a, b, index

    def _save(self, state: MatchingState) -> MatchingState:
        """原子保存当前任务头及工作进度。

        Args:
            state: 经过领域模型校验的完整状态。

        Returns:
            已成功持久化的同一状态。
        """
        self.repository.write_model(state.task_id, "matching/state.json", state)
        return state

    def get(self, task_id: str) -> MatchingState:
        """查询进度并拒绝已过期的第一块输入。

        Args:
            task_id: 已创建匹配任务的编号，与第一块一致。

        Returns:
            最新原子状态；尚未开始时返回 404。
        """
        state = self.repository.read_model(task_id, "matching/state.json", MatchingState)
        task = self.repository.get_task(task_id)
        if (
            task.data_version != state.data_version
            or task.prepared_ref != state.prepared_data_ref.relative_path
        ):
            raise PreparationError(
                "STALE_PREPARED_DATA", "第一块数据已变化，当前匹配方案已过期", http_status=409
            )
        self._verify(task_id, state.prepared_data_ref)
        return state

    def run(self, task_id: str, rules: MatchingRules | None = None) -> MatchingState:
        """启动或恢复第二块；已发布任务重跑不重复消费 B。

        Args:
            task_id: 第一块就绪任务。
            rules: 初次执行的明确规则，后续变更必须走带审计的确认入口。

        Returns:
            ready、needs_input 或 failed 的真实匹配状态。
        """
        try:
            with self.repository.lock(task_id):
                prepared, reference, a, b, index = self._inputs(task_id)
                state_path = self.repository.path(task_id, "matching/state.json")
                if state_path.is_file():
                    state = self.get(task_id)
                    if rules is not None and rules != state.rules:
                        raise PreparationError(
                            "RULE_CONFIRMATION_REQUIRED",
                            "规则更新需提交确认人和原因",
                            http_status=409,
                        )
                    if state.status in {"ready", "needs_input"}:
                        return state
                else:
                    selected_rules = rules or MatchingRules(
                        date_tolerance_days=prepared.task_context.rules.date_tolerance_days,
                        specification_aliases=prepared.task_context.rules.specification_aliases,
                    )
                    self._validate_rules(selected_rules)
                    state = self._save(
                        MatchingState(
                            task_id=task_id,
                            prepared_data_ref=reference,
                            data_version=prepared.task_context.data_version,
                            rules=selected_rules,
                            total_rows=len(a.records),
                        )
                    )
                return self._execute(state, prepared, a.records, b.records, index)
        except Timeout as exc:
            raise PreparationError(
                "TASK_BUSY", "同一任务正在执行，请稍后查询", http_status=409
            ) from exc

    def _validate_rules(self, rules: MatchingRules) -> None:
        """检查规则别名链，拒绝不终止或空值映射。

        Args:
            rules: 初次创建或人工更新的完整规则。
        """
        try:
            for source, target in rules.specification_aliases.items():
                if not source.strip() or not target.strip():
                    raise ValueError("别名不能为空")
                canonical(source, rules)
        except ValueError as exc:
            raise PreparationError("INVALID_MATCH_RULES", str(exc)) from exc

    def _pool(
        self, current: BusinessRecord, records: tuple[BusinessRecord, ...], index: LookupIndex
    ) -> tuple[BusinessRecord, ...]:
        """通过 B 索引获取有业务依据的来源池，并保留原始顺序。

        Args:
            current: 当前 A 行。
            records: 全部真实 B 方向记录。
            index: 与记录集合一致的第一块索引。

        Returns:
            同方向或零数量未知方向的候选池，身份相同行不会去重。
        """
        if current.direction.value == "unknown":
            return records
        ids = set(index.by_direction.get(current.direction.value, ()))
        if current.normalized_fields.quantity == "0":
            ids.update(index.by_direction.get("unknown", ()))
        return tuple(r for r in records if r.record_id in ids)

    def _execute(
        self,
        state: MatchingState,
        prepared: PreparedData,
        a: tuple[BusinessRecord, ...],
        b: tuple[BusinessRecord, ...],
        index: LookupIndex,
    ) -> MatchingState:
        """逐行保存进度，整表检查通过后最后发布 MatchPlan 指针。

        Args:
            state: 当前匹配工作版本，失败恢复时保留已有行检查点。
            prepared: 已校验的当前输入包。
            a: 原始顺序 A 明细。
            b: 完整 B 明细。
            index: 完整校验通过的 B 查询索引。

        Returns:
            已原子发布的最终状态，工具失败时保存 failed 及可恢复进度。
        """
        state = self._save(state.model_copy(update={"status": "running", "issues": ()}))
        try:
            rows = {r.a_record_id: r for r in state.progress}
            pending = set(
                state.pending_rows or tuple(r.record_id for r in a if r.record_id not in rows)
            )
            state = self._save(
                state.model_copy(
                    update={"pending_rows": tuple(r.record_id for r in a if r.record_id in pending)}
                )
            )
            excluded = {r.b_record_id for r in state.exclusions}
            for current in a:
                if current.record_id not in pending:
                    continue
                pool = tuple(
                    r for r in self._pool(current, b, index) if r.record_id not in excluded
                )
                known = {c.candidate_id: c for row in rows.values() for c in row.candidates}
                confirmed_usage = tuple(
                    dict.fromkeys(
                        identifier
                        for key in manual_candidates(state, known)
                        for identifier in known[key].b_record_ids
                    )
                )
                result = self.workflow.invoke(
                    RowState(
                        current=current,
                        a_records=a,
                        b_records=pool,
                        rules=state.rules,
                        progress=rows.get(
                            current.record_id, RowProgress(a_record_id=current.record_id)
                        ),
                        confirmations=state.confirmations,
                        b_usage=confirmed_usage,
                    )
                )
                rows[current.record_id] = result["progress"]
                pending.remove(current.record_id)
                state = self._save(
                    state.model_copy(
                        update={
                            "progress": tuple(rows.values()),
                            "completed_rows": len(rows),
                            "pending_rows": tuple(r.record_id for r in a if r.record_id in pending),
                        }
                    )
                )
            artifacts = reconcile(state, a, b)
            return self._publish(state, prepared, artifacts)
        except Exception as exc:
            logger.exception("Matching task failed: %s", state.task_id)
            issue = Issue(
                code=exc.code if isinstance(exc, PreparationError) else "MATCH_TOOL_FAILED",
                message=exc.message
                if isinstance(exc, PreparationError)
                else "匹配工具执行失败，可从行检查点恢复",
            )
            return self._save(state.model_copy(update={"status": "failed", "issues": (issue,)}))

    def _publish(
        self, state: MatchingState, prepared: PreparedData, result: MatchArtifacts
    ) -> MatchingState:
        """发布自包含的版本目录，全部写完后原子切换任务头。

        Args:
            state: 当前工作版本及审计信息。
            prepared: 匹配所依据的第一块版本。
            result: 整表检查通过的正式关系和未确定事项。

        Returns:
            已公开新 MatchPlan 引用的最新状态。
        """
        base = f"matching/versions/{state.version}"
        values: dict[str, DomainModel] = {
            "a_row_results": RowsArtifact(rows=result.rows),
            "match_groups": GroupsArtifact(groups=result.groups),
            "b_usage": UsageArtifact(records=result.b_usage),
            "review_items": ReviewsArtifact(items=result.reviews),
            "exclusions": ExclusionsArtifact(records=result.exclusions),
            "execution_issues": IssuesArtifact(issues=result.execution_issues),
            "confirmations": ConfirmationsArtifact(records=state.confirmations),
            "progress": ProgressArtifact(rows=state.progress),
        }
        refs = {
            key: self.repository.write_model(state.task_id, f"{base}/{key}.json", value)
            for key, value in values.items()
        }
        readiness: Literal["ready", "needs_input"] = (
            "needs_input" if any(i.blocking for i in result.execution_issues) else "ready"
        )
        plan = MatchPlan(
            task_id=state.task_id,
            prepared_data_ref=state.prepared_data_ref,
            prepared_data_version=state.data_version,
            preparation_rule_version=prepared.task_context.rule_version,
            rule_version=state.rules.version,
            rules=state.rules,
            match_plan_version=state.version,
            a_row_results_ref=refs["a_row_results"],
            match_groups_ref=refs["match_groups"],
            b_usage_ref=refs["b_usage"],
            review_items_ref=refs["review_items"],
            exclusions_ref=refs["exclusions"],
            execution_issues_ref=refs["execution_issues"],
            confirmations_ref=refs["confirmations"],
            progress_ref=refs["progress"],
            readiness=readiness,
        )
        plan_ref = self.repository.write_model(state.task_id, f"{base}/match_plan.json", plan)
        return self._save(
            state.model_copy(
                update={
                    "status": readiness,
                    "plan_ref": plan_ref,
                    "issues": result.execution_issues,
                }
            )
        )

    def plan(self, task_id: str, *, expected_version: int | None = None) -> MatchPlan:
        """读取第三块的最新正式输入，拒绝旧版本或未完成提交。

        Args:
            task_id: 匹配任务编号。
            expected_version: 第三块持有的匹配版本，可用于防止陈旧制表。

        Returns:
            摘要及版本均有效的最新 MatchPlan。
        """
        state = self.get(task_id)
        self._inputs(task_id)
        if expected_version is not None and expected_version != state.version:
            raise PreparationError("STALE_MATCH_PLAN", "匹配方案版本已变化", http_status=409)
        if state.plan_ref is None or state.status not in {"ready", "needs_input"}:
            raise PreparationError("MATCH_PLAN_NOT_READY", "匹配方案尚未发布", http_status=409)
        self._verify(task_id, state.plan_ref)
        plan = self.repository.read_model(task_id, state.plan_ref.relative_path, MatchPlan)
        if (
            plan.match_plan_version != state.version
            or plan.prepared_data_ref != state.prepared_data_ref
        ):
            raise PreparationError(
                "STALE_MATCH_PLAN", "匹配方案与当前任务版本不一致", http_status=409
            )
        for ref in self._plan_refs(plan):
            self._verify(task_id, ref)
        return plan

    def _plan_refs(self, plan: MatchPlan) -> tuple[ArtifactRef, ...]:
        """列出正式交接文件，供验证及白名单下载。

        Args:
            plan: 已发布版本的方案。

        Returns:
            同一匹配版本的全部结果文件引用。
        """
        return (
            plan.a_row_results_ref,
            plan.match_groups_ref,
            plan.b_usage_ref,
            plan.review_items_ref,
            plan.exclusions_ref,
            plan.execution_issues_ref,
            plan.confirmations_ref,
            plan.progress_ref,
        )

    def artifacts(self, task_id: str) -> MatchArtifacts:
        """独立加载完整 MatchPlan 结果，用于第三块和人工复核。

        Args:
            task_id: 已发布匹配任务。

        Returns:
            正式关系、余额、行结果和审计问题。
        """
        plan = self.plan(task_id)
        return MatchArtifacts(
            match_plan_version=plan.match_plan_version,
            rows=self.repository.read_model(
                task_id, plan.a_row_results_ref.relative_path, RowsArtifact
            ).rows,
            groups=self.repository.read_model(
                task_id, plan.match_groups_ref.relative_path, GroupsArtifact
            ).groups,
            b_usage=self.repository.read_model(
                task_id, plan.b_usage_ref.relative_path, UsageArtifact
            ).records,
            reviews=self.repository.read_model(
                task_id, plan.review_items_ref.relative_path, ReviewsArtifact
            ).items,
            exclusions=self.repository.read_model(
                task_id, plan.exclusions_ref.relative_path, ExclusionsArtifact
            ).records,
            execution_issues=self.repository.read_model(
                task_id, plan.execution_issues_ref.relative_path, IssuesArtifact
            ).issues,
        )

    def artifact(self, task_id: str, name: str, expected_version: int) -> Path:
        """下载当前版本白名单文件，防止旧版被误称为最新结果。

        Args:
            task_id: 匹配任务编号。
            name: 公开清单文件名。
            expected_version: 页面显示或第三块持有的方案版本。

        Returns:
            内容摘要验证通过的绝对文件路径。
        """
        plan = self.plan(task_id, expected_version=expected_version)
        state = self.get(task_id)
        refs = self._plan_refs(plan) + ((state.plan_ref,) if state.plan_ref is not None else ())
        reference = next((r for r in refs if r.name == name), None)
        if reference is None:
            raise PreparationError("ARTIFACT_NOT_FOUND", "匹配产物不存在", http_status=404)
        return self.repository.path(task_id, reference.relative_path)

    def confirm(self, task_id: str, confirmation: MatchConfirmation) -> MatchingState:
        """校验人工操作后产生新方案版本，并重新计算全部影响关系。

        Args:
            task_id: 当前匹配任务。
            confirmation: 处理对象、确认人、业务原因和预期版本。

        Returns:
            新版本匹配状态；非法选择不会修改当前状态。
        """
        try:
            with self.repository.lock(task_id):
                state = self.get(task_id)
                if state.status not in {"ready", "needs_input"}:
                    raise PreparationError(
                        "MATCH_NOT_WAITING", "匹配任务尚未发布，不能确认", http_status=409
                    )
                if confirmation.expected_match_plan_version != state.version:
                    raise PreparationError(
                        "STALE_CONFIRMATION", "确认版本已过期，请重新查询", http_status=409
                    )
                prepared, _, a, b, index = self._inputs(task_id)
                decision = confirmation.decision
                candidates = {c.candidate_id: c for row in state.progress for c in row.candidates}
                if decision.action == "accept" and decision.candidate_id not in candidates:
                    raise PreparationError("INVALID_MATCH_REFERENCE", "人工确认必须选择当前候选")
                if not set(decision.a_record_ids).issubset(
                    {r.record_id for r in a.records}
                ) or not set(decision.b_record_ids).issubset({r.record_id for r in b.records}):
                    raise PreparationError("INVALID_MATCH_REFERENCE", "人工处理对象不存在")
                record = MatchConfirmationRecord(
                    confirmation_id=str(uuid4()),
                    confirmation=confirmation,
                    confirmed_at=datetime.now(UTC),
                    output_version=state.version + 1,
                )
                exclusions = {r.b_record_id: r for r in state.exclusions}
                if decision.action == "exclude_b":
                    for identifier in decision.b_record_ids:
                        exclusions[identifier] = Exclusion(
                            b_record_id=identifier,
                            reason=confirmation.reason,
                            confirmation_ref=record.confirmation_id,
                        )
                elif decision.action == "restore_b":
                    for identifier in decision.b_record_ids:
                        exclusions.pop(identifier, None)
                new_rules = decision.rules or state.rules
                self._validate_rules(new_rules)
                if (
                    decision.rules is not None
                    and decision.rules != state.rules
                    and decision.rules.version == state.rules.version
                ):
                    raise PreparationError(
                        "RULE_VERSION_REQUIRED", "规则内容变化必须使用新版本标识"
                    )
                updated = state.model_copy(
                    update={
                        "version": state.version + 1,
                        "rules": new_rules,
                        "confirmations": (*state.confirmations, record),
                        "exclusions": tuple(exclusions.values()),
                        "plan_ref": None,
                    }
                )
                if decision.action == "rules":
                    updated = updated.model_copy(update={"progress": (), "completed_rows": 0})
                elif decision.action in {"exclude_b", "restore_b"}:
                    # 全部重查避免保留依赖已排除 B 的旧模型选择。
                    updated = updated.model_copy(update={"progress": (), "completed_rows": 0})
                # 在写入任何新状态前验证人工选择不能绕过数值和容量限制。
                reconcile(updated, a.records, b.records)
                return self._execute(self._save(updated), prepared, a.records, b.records, index)
        except Timeout as exc:
            raise PreparationError("TASK_BUSY", "同一任务正在执行", http_status=409) from exc

    def continue_search(self, task_id: str, expected_version: int) -> MatchingState:
        """在原规则下继续预算未完成搜索，保留游标并发布新版本。

        Args:
            task_id: 有未完成搜索的匹配任务。
            expected_version: 调用方持有的当前版本，避免覆盖人工修改。

        Returns:
            继续搜索后重新统一仲裁的新版本状态。
        """
        try:
            with self.repository.lock(task_id):
                state = self.get(task_id)
                if state.version != expected_version or state.status not in {
                    "ready",
                    "needs_input",
                }:
                    raise PreparationError(
                        "STALE_MATCH_PLAN", "任务状态或方案版本已变化", http_status=409
                    )
                if all(row.search_complete for row in state.progress):
                    return state
                prepared, _, a, b, index = self._inputs(task_id)
                updated = state.model_copy(
                    update={
                        "version": state.version + 1,
                        "pending_rows": tuple(
                            row.a_record_id for row in state.progress if not row.search_complete
                        ),
                        "plan_ref": None,
                    }
                )
                return self._execute(self._save(updated), prepared, a.records, b.records, index)
        except Timeout as exc:
            raise PreparationError("TASK_BUSY", "同一任务正在执行", http_status=409) from exc
