"""每条 A 均经过的 LangGraph 行工作流；外围按行原子保存检查点。"""

import logging
from typing import NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.matching.advisor import MatchingAdvisor
from app.matching.models import (
    AliasQueryProgress,
    AliasSuggestion,
    MatchConfirmationRecord,
    MatchingRules,
    ModelProposal,
    ProposalTrace,
    RowContext,
    RowProgress,
)
from app.matching.prompts import build_matching_prompt
from app.matching.search import canonical, identity_compatible, search_row
from app.preparation.domain.errors import PreparationError
from app.preparation.domain.models import BusinessRecord, Issue

logger = logging.getLogger(__name__)


class RowState(TypedDict):
    """图内只携带已类型化上下文和单行进度。"""

    current: BusinessRecord
    a_records: tuple[BusinessRecord, ...]
    b_records: tuple[BusinessRecord, ...]
    rules: MatchingRules
    progress: RowProgress
    b_usage: NotRequired[tuple[str, ...]]
    confirmations: NotRequired[tuple[MatchConfirmationRecord, ...]]


class RowWorkflow:
    """规则查询、必要的语义建议、程序验证组成一条行任务。"""

    def __init__(self, advisor: MatchingAdvisor | None) -> None:
        """注入可选的模型能力。

        Args:
            advisor: 语义复核端口；未配置时疑难项保留人工入口。
        """
        self.advisor = advisor

    def build(self) -> CompiledStateGraph[RowState, None, RowState, RowState]:
        """编译所有 A 行统一执行的工作流。

        Returns:
            先执行程序查询，再按条件调用模型的已编译图。
        """
        graph = StateGraph(RowState)
        graph.add_node("search", self._search)
        graph.add_node("advise", self._advise)
        graph.add_edge(START, "search")
        graph.add_edge("search", "advise")
        graph.add_edge("advise", END)
        return graph.compile()

    def _search(self, state: RowState) -> RowState:
        """按持久化游标推进规则搜索。

        Args:
            state: 当前行、真实来源和同版本进度。

        Returns:
            包含新的候选和搜索完整性标记的图状态。
        """
        progress = state["progress"]
        if not progress.base_search_complete:
            progress = search_row(
                state["current"], state["a_records"], state["b_records"], state["rules"], progress
            )
        for query in progress.alias_queries:
            if (
                not query.search_complete
                and progress.tool_queries < state["rules"].max_tool_queries
            ):
                progress = self._query(state, progress, query.alias, query.cursor)
        progress = self._search_status(progress, state["rules"])
        return RowState(**{**state, "progress": progress})

    def _search_status(self, progress: RowProgress, rules: MatchingRules) -> RowProgress:
        """汇总主规则和行级别名查询的完整性，查询上限不能被伪装为搜完。

        Args:
            progress: 主规则和各语义查询的独立检查点。
            rules: 当前行的工具调用次数上限。

        Returns:
            保留全部未完成范围的行检查点。
        """
        reasons = (
            list(progress.search_incomplete_reasons) if not progress.base_search_complete else []
        )
        for query in progress.alias_queries:
            if not query.search_complete:
                reasons.extend(f"alias_{reason}" for reason in query.incomplete_reasons)
                if progress.tool_queries >= rules.max_tool_queries:
                    reasons.append("tool_query_limit")
        return progress.model_copy(
            update={
                "search_complete": progress.base_search_complete
                and all(q.search_complete for q in progress.alias_queries),
                "search_incomplete_reasons": tuple(dict.fromkeys(reasons)),
            }
        )

    def _query(
        self, state: RowState, progress: RowProgress, alias: AliasSuggestion, cursor: int
    ) -> RowProgress:
        """执行有独立游标的当前行语义检索，仍用原始数值规则重算候选。

        Args:
            state: 当前真实来源和固定版本的正式规则。
            progress: 包括已用查询次数的行检查点。
            alias: 已经核对真实来源的规格查询建议。
            cursor: 同一别名范围的上次搜索位置。

        Returns:
            合并去重候选、查询次数和语义检索游标后的新检查点。
        """
        rules = state["rules"]
        scoped = rules.model_copy(
            update={
                "specification_aliases": {**rules.specification_aliases, alias.source: alias.target}
            }
        )
        queried = tuple(
            r for r in state["b_records"] if identity_compatible(state["current"], r, scoped)
        )
        discovered = search_row(
            state["current"],
            state["a_records"],
            queried,
            rules,
            RowProgress(
                a_record_id=state["current"].record_id,
                cursor=cursor,
                candidates=tuple(c for c in progress.candidates if c.search_alias == alias),
            ),
            alias,
        )
        merged = {c.candidate_id: c for c in (*progress.candidates, *discovered.candidates)}
        complete = discovered.search_complete and len(merged) <= rules.max_candidates
        reasons = discovered.search_incomplete_reasons + (
            ("candidate_limit",) if len(merged) > rules.max_candidates else ()
        )
        query = AliasQueryProgress(
            alias=alias,
            cursor=discovered.cursor,
            search_complete=complete,
            incomplete_reasons=reasons,
        )
        queries = tuple(q for q in progress.alias_queries if q.alias != alias) + (query,)
        updated = progress.model_copy(
            update={
                "candidates": tuple(merged.values())[: rules.max_candidates],
                "tool_queries": progress.tool_queries + 1,
                "alias_queries": queries,
            }
        )
        return self._search_status(updated, rules)

    def _context(self, state: RowState) -> RowContext:
        """构建有限的当前行、相邻和候选来源证据。

        Args:
            state: 当前规则、候选及真实记录。

        Returns:
            最多五条相邻 A、二十条 B 和三十个候选的模型输入。
        """
        position = next(
            i for i, r in enumerate(state["a_records"]) if r.record_id == state["current"].record_id
        )
        candidates = tuple(sorted(state["progress"].candidates, key=lambda c: not c.eligible))[:30]
        ids = {i for c in candidates for i in c.b_record_ids}
        records = tuple(r for r in state["b_records"] if r.record_id in ids)[:20]
        if not records:
            # 无现成候选时只展示同方向、日期范围内的真实来源，供提出查询建议。
            records = tuple(
                r for r in state["b_records"] if r.direction == state["current"].direction
            )[:20]
        return RowContext(
            a_record=state["current"],
            neighbors=state["a_records"][max(0, position - 2) : position + 3],
            b_records=records,
            candidates=candidates,
            rules=state["rules"],
            b_usage=state.get("b_usage", ()),
            confirmations=state.get("confirmations", ()),
        )

    def _validate_proposal(self, proposal: ModelProposal, context: RowContext) -> None:
        """校验模型的来源引用和候选资格，不接受编造的关系。

        Args:
            proposal: 模型返回的建议。
            context: 该轮实际提供给模型的有限证据。
        """
        evidence = {r.record_id for r in (context.a_record, *context.neighbors, *context.b_records)}
        if not set(proposal.evidence).issubset(evidence):
            raise PreparationError("INVALID_MODEL_EVIDENCE", "模型引用了输入中不存在的证据")
        if proposal.candidate_id:
            candidate = next(
                (c for c in context.candidates if c.candidate_id == proposal.candidate_id), None
            )
            if candidate is None or not candidate.eligible:
                raise PreparationError(
                    "INVALID_MODEL_CANDIDATE", "模型采用了不存在或未通过数值校验的候选"
                )
            if not proposal.evidence or not set(proposal.evidence).intersection(
                candidate.b_record_ids
            ):
                raise PreparationError("MISSING_MODEL_EVIDENCE", "模型选择缺少对应 B 来源证据")
            if candidate.requires_advice and not proposal.specification_equivalent:
                raise PreparationError(
                    "UNCONFIRMED_SPECIFICATION", "规格不同但模型没有明确语义对应依据"
                )
        alias = proposal.suggested_alias
        if alias is not None and (
            alias.source != context.a_record.normalized_fields.spec
            or alias.target not in {r.normalized_fields.spec for r in context.b_records}
            or not alias.evidence
            or not set(alias.evidence).issubset(evidence)
            or not set(alias.evidence).intersection({r.record_id for r in context.b_records})
        ):
            raise PreparationError(
                "INVALID_ALIAS_SUGGESTION", "新别名必须对应当前 A 和真实 B 规格及来源证据"
            )
        if alias is not None:
            canonical(
                alias.source,
                context.rules.model_copy(
                    update={
                        "specification_aliases": {
                            **context.rules.specification_aliases,
                            alias.source: alias.target,
                        }
                    }
                ),
            )

    def _advise(self, state: RowState) -> RowState:
        """只对疑难行调用模型，并限制轮次及追加查询次数。

        Args:
            state: 已执行程序检索的行状态。

        Returns:
            保存输入输出、建议校验、查询次数和执行问题的状态。
        """
        progress, rules = state["progress"], state["rules"]
        eligible = [c for c in progress.candidates if c.eligible and c.tier != "partial"]
        if self.advisor is None or (len(eligible) == 1 and not eligible[0].requires_advice):
            return state
        while len(progress.proposals) < rules.max_model_rounds:
            context = self._context(RowState(**{**state, "progress": progress}))
            prompt = build_matching_prompt(context)
            proposal: ModelProposal | None = None
            error: str | None = None
            try:
                proposal = self.advisor.suggest(context)
                self._validate_proposal(proposal, context)
            except Exception as exc:
                logger.exception("Matching semantic advice failed")
                error = exc.code if isinstance(exc, PreparationError) else "MATCH_MODEL_FAILED"
            trace = ProposalTrace(
                round=len(progress.proposals) + 1,
                context=context,
                proposal=proposal,
                error=error,
                prompt_id=prompt.prompt_id,
                prompt_version=prompt.version,
                prompt_sha256=prompt.template_sha256,
            )
            issues = progress.execution_issues
            if error:
                issues += (
                    Issue(
                        code=error,
                        message="语义建议未通过程序校验或调用失败，已保留候选",
                        blocking=False,
                        source_ref=state["current"].record_id,
                    ),
                )
            progress = progress.model_copy(
                update={"proposals": (*progress.proposals, trace), "execution_issues": issues}
            )
            if error or proposal is None:
                break
            if proposal.candidate_id:
                progress = progress.model_copy(
                    update={"selected_candidate_id": proposal.candidate_id}
                )
                break
            alias = proposal.suggested_alias
            if alias is None:
                break
            if progress.tool_queries >= rules.max_tool_queries:
                progress = progress.model_copy(
                    update={
                        "execution_issues": (
                            *progress.execution_issues,
                            Issue(
                                code="MODEL_TOOL_BUDGET",
                                message="语义查询次数已达上限，建议及候选已保留",
                                blocking=False,
                                source_ref=state["current"].record_id,
                            ),
                        )
                    }
                )
                break
            previous = next((q for q in progress.alias_queries if q.alias == alias), None)
            if previous is not None and previous.search_complete:
                break
            progress = self._query(state, progress, alias, previous.cursor if previous else 0)
        return RowState(**{**state, "progress": progress})
