"""通过独立 Excel 库生成/重读样例，验证完整制表及验收失败门槛。"""

import io
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook, load_workbook

from app.core.config import Settings
from app.delivery.generate import CGenerator
from app.delivery.models import (
    DeliveryInputs,
    DeliveryOptions,
    DeliveryRequest,
    GenerationManifest,
    VerificationReport,
)
from app.delivery.ooxml import METADATA_HEADERS, column
from app.delivery.service import DeliveryService
from app.matching.models import ManualDecision, MatchConfirmation, MatchingRules
from app.matching.service import MatchingService
from app.preparation.application.service import PreparationService
from app.preparation.domain.errors import PreparationError
from app.preparation.domain.models import TaskOptions
from app.preparation.infrastructure.xlsx import tag
from tests.workbooks import a_workbook, b_workbook, replace_cell_xml


def simple_books(
    a_quantities: tuple[int, ...],
    b_quantity: int,
    *,
    a_day: int = 1,
    b_month: int = 7,
    b_day: int = 1,
) -> tuple[bytes, bytes]:
    """创建无额外记录的多对一/部分/跨月样例，数值文本也保留原始类型。

    Args:
        a_quantities: 各条 A 业务原始数量。
        b_quantity: 单条 B 原始数量。
        a_day: A 的七月日期。
        b_month: B 系统月份。
        b_day: B 系统日期。

    Returns:
        独立 Excel 库生成的 A/B 工作簿字节。
    """
    a = Workbook()
    sheet = a.worksheets[0]
    sheet.title = "业务底稿"
    sheet.append(["七月"])
    sheet.append(["日期", "产品名称", "数量", "单价", "金额"])
    for q in a_quantities:
        sheet.append([datetime(2026, 7, a_day), "支架", str(q), "2", str(q * 2)])
    end = len(a_quantities) + 2
    sheet.append(["合计", None, f"=SUM(C3:C{end})", None, f"=SUM(E3:E{end})"])
    b = load_workbook(io.BytesIO(b_workbook()))
    system = b.worksheets[0]
    system.delete_rows(2, system.max_row)
    system.append(
        [
            1,
            "公司",
            "部门",
            "经办",
            "配货仓",
            datetime(2026, b_month, b_day),
            "DOC1",
            "SKU1",
            "支架",
            "支架",
            str(b_quantity),
            "2",
            str(b_quantity * 2),
            "0",
            "0",
            "0",
            "2",
        ]
    )
    a_buffer, b_buffer = io.BytesIO(), io.BytesIO()
    a.save(a_buffer)
    b.save(b_buffer)
    return a_buffer.getvalue(), b_buffer.getvalue()


def split_b() -> bytes:
    """在原系统样例末尾追加非连续拆分记录，保留重复来源、退货、零值和双向行。

    Returns:
        含 449+13 非连续组合及未承接重复行的真实 B 工作簿。
    """
    workbook = load_workbook(io.BytesIO(b_workbook()))
    sheet = workbook.worksheets[0]
    values = [sheet.cell(2, col).value for col in range(1, 18)]
    values[0], values[10], values[12] = 6, 13, 205.007738
    sheet.append(values)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def delivery_task(
    path: Path, *, a_content: bytes | None = None, b_content: bytes | None = None
) -> tuple[str, DeliveryService, int]:
    """执行真实第一、第二块并确认其中一组非连续正式关系。

    Args:
        path: pytest 或本地回归专用的隔离目录。
        a_content: 可选 A 工作簿变体。
        b_content: 可选 B 工作簿变体。

    Returns:
        任务编号、第三块服务和真实匹配版本。
    """
    settings = Settings(storage_root=path, llm_model=None, llm_api_key=None)
    preparation = PreparationService(settings)
    task = preparation.create(
        "a.xlsx",
        a_content or a_workbook(),
        "b.xlsx",
        b_content or split_b(),
        TaskOptions(
            reconciliation_period="2026-07",
            specification_aliases={"支架站脚款": "支架站脚", "支架滑轮款": "支架滑轮"},
        ),
    )
    assert preparation.run(task.task_id).status.value == "ready"
    matching = MatchingService(settings)
    state = matching.run(task.task_id)
    assert state.status == "ready"
    candidates = [
        c
        for row in state.progress
        for c in row.candidates
        if c.eligible and len(c.b_record_ids) == 2 and c.relation_type == "one_to_many"
    ]
    if candidates:
        candidate = next(c for c in candidates if any(":2:" in i for i in c.b_record_ids))
        state = matching.confirm(
            task.task_id,
            MatchConfirmation(
                actor="业务复核员",
                reason="已核对原始行2与行8对应拆分单据",
                expected_match_plan_version=state.version,
                decision=ManualDecision(action="accept", candidate_id=candidate.candidate_id),
            ),
        )
    return task.task_id, DeliveryService(settings), state.version


def workbook_path(service: DeliveryService, task_id: str, generation: int, kind: str) -> Path:
    """读取实际登记路径，避免测试绕过最终版本和摘要校验。

    Args:
        service: 本次隔离的第三块服务。
        task_id: 真实任务编号。
        generation: 当前生成版本。
        kind: 要读取的文件类型。

    Returns:
        实际最终文件绝对路径。
    """
    result = service.result(task_id, generation)
    artifact = next(a for a in result.artifacts if a.kind == kind)
    return service.repository.path(task_id, artifact.reference.relative_path)


def mutate_cell(
    path: Path, sheet_path: str, coordinate: str, value: str, *, formula: str | None = None
) -> None:
    """在实际输出上模拟精度损坏或错误缓存，不通过生产生成器改写。

    Args:
        path: 待模拟损坏的输出文件。
        sheet_path: 实际 ZIP 工作表部件。
        coordinate: 输出单元格坐标。
        value: 需要写入的错误数值或缓存。
        formula: 可选的错误公式字符串。
    """
    with ZipFile(path) as source:
        parts = {n: source.read(n) for n in source.namelist()}
    root = ET.fromstring(parts[sheet_path])
    cell = next(c for c in root.iter(tag("c")) if c.get("r") == coordinate)
    cell.set("t", "n")
    for child in list(cell):
        if child.tag in {tag("v"), tag("f"), tag("is")}:
            cell.remove(child)
    if formula is not None:
        ET.SubElement(cell, tag("f")).text = formula
    ET.SubElement(cell, tag("v")).text = value
    parts[sheet_path] = ET.tostring(root, encoding="utf-8")
    with ZipFile(path, "w", ZIP_DEFLATED) as output:
        for name, content in parts.items():
            output.writestr(name, content)


def test_delivery_real_workbooks_expansion_precision_totals_and_coverage(tmp_path: Path) -> None:
    """真实 C/B 形成三类交付，非连续展开不重复 A，双向与原行分别计数。

    Args:
        tmp_path: pytest 隔离存储目录。
    """
    task_id, service, match_version = delivery_task(tmp_path)
    state = service.run(task_id, DeliveryRequest(expected_match_plan_version=match_version))
    assert state.status == "completed_with_review", [
        (i.code, i.output_location, i.actual) for i in state.issues
    ]
    result = service.result(task_id, state.generation_version)
    statistics = result.statistics
    assert statistics.b_current_month_original_rows == 6
    assert statistics.b_current_month_business_records == 7
    assert (
        statistics.taken_business_records,
        statistics.missing_business_records,
        statistics.excluded_business_records,
    ) == (4, 3, 0)
    assert statistics.outside_period_business_records == 1
    assert all(t.quantity_difference == t.amount_difference == "0" for t in statistics.totals)
    c_path = workbook_path(service, task_id, 1, "c_workbook")
    c = load_workbook(c_path, data_only=False)
    sheet = c["业务底稿"]
    assert [sheet.cell(4, col).value for col in range(1, 6)] == [None] * 5
    assert sheet["C3"].value == 462 and sheet["C6"].value == -3
    assert sheet["C7"].data_type == "f" and "C6" in str(sheet["C7"].value)
    assert c["原始附件"]["A1"].value == "完整保留此工作表"
    assert next(iter(c["原始附件"].merged_cells.ranges)).coord == "A3:B3"
    assert str(sheet.freeze_panes) == "A3" and sheet.sheet_view.topLeftCell == "A1"
    assert sheet.auto_filter.ref is not None
    cached = load_workbook(c_path, data_only=True)["业务底稿"]
    assert cached["C7"].value == 459 and str(cached["E7"].value) == "7235.511612"
    report = service.repository.read_model(
        task_id, result.verification_report_ref.relative_path, VerificationReport
    )
    assert report.passed and all(check.status == "pass" for check in report.checks)
    b = load_workbook(workbook_path(service, task_id, 1, "annotated_b"), data_only=True).worksheets[
        0
    ]
    assert b.max_row == 8 and b["K2"].value == 449 and b["L2"].value == 15.769826
    assert "入库：C表缺失" in str(b["R7"].value) and "出库：C表缺失" in str(b["R7"].value)
    assert b["R6"].value == "非本月"
    assert "人工表有" not in str(b["W5"].value)
    assert service.run(task_id, DeliveryRequest(expected_match_plan_version=match_version)) == state
    assert service.recheck(task_id, 1).status == "completed_with_review"


@pytest.mark.parametrize("capability", ["formula_engine", "display_engine"])
def test_unavailable_mandatory_capability_blocks_final_download(
    tmp_path: Path, capability: str
) -> None:
    """未执行的强制检查不会登记为通过，保留独立诊断下载。

    Args:
        tmp_path: 隔离存储目录。
        capability: 本轮关闭的明确验收能力。
    """
    task_id, service, match_version = delivery_task(tmp_path)
    options = (
        DeliveryOptions(formula_engine="unavailable")
        if capability == "formula_engine"
        else DeliveryOptions(display_engine="unavailable")
    )
    state = service.run(
        task_id, DeliveryRequest(expected_match_plan_version=match_version, options=options)
    )
    assert state.status == "needs_input"
    with pytest.raises(PreparationError, match="只.*诊断"):
        service.artifact(task_id, "c_reconciled.xlsx", 1)
    path = service.artifact(task_id, "verification_report.json", 1, diagnostic=True)
    report = VerificationReport.model_validate_json(path.read_bytes())
    assert not report.passed and any(c.status == "not_run" for c in report.checks)


def test_recheck_detects_real_b_precision_damage_and_revokes_delivery(tmp_path: Path) -> None:
    """实际 C 追溯精度受损不能用补缺失标注掩盖，已登记交付立即失效。

    Args:
        tmp_path: 隔离存储目录。
    """
    task_id, service, match_version = delivery_task(tmp_path)
    state = service.run(task_id, DeliveryRequest(expected_match_plan_version=match_version))
    assert state.status == "completed_with_review"
    c_path = workbook_path(service, task_id, 1, "c_workbook")
    result = service.result(task_id, 1)
    manifest_ref = next(a.reference for a in result.artifacts if a.kind == "manifest")
    manifest = service.repository.read_model(
        task_id, manifest_ref.relative_path, GenerationManifest
    )
    layout = manifest.layouts[0]
    price_index = next(i for i, d in enumerate(layout.trace_columns) if d.field.value == "in_price")
    mutate_cell(
        c_path,
        "xl/worksheets/sheet1.xml",
        f"{column(layout.original_columns + len(METADATA_HEADERS) + price_index + 1)}3",
        "15.77",
    )
    with pytest.raises(PreparationError, match="摘要"):
        service.artifact(task_id, "c_reconciled.xlsx", 1)
    checked = service.recheck(task_id, 1)
    assert checked.status == "failed"
    assert any(i.code == "B_TRACE_VALUE_CHANGED" for i in checked.issues)
    with pytest.raises(PreparationError):
        service.artifact(task_id, "c_reconciled.xlsx", 1)
    rebuilt = service.run(task_id, DeliveryRequest(expected_match_plan_version=match_version))
    assert rebuilt.generation_version == 2 and rebuilt.status == "completed_with_review"


def test_old_aggregate_range_with_self_consistent_cache_is_rejected(tmp_path: Path) -> None:
    """旧范围即使已重新计算缓存，也不能遗漏插行后的最后业务行。

    Args:
        tmp_path: 隔离存储目录。
    """
    task_id, service, match_version = delivery_task(tmp_path)
    service.run(task_id, DeliveryRequest(expected_match_plan_version=match_version))
    c_path = workbook_path(service, task_id, 1, "c_workbook")
    mutate_cell(c_path, "xl/worksheets/sheet1.xml", "C7", "462", formula="SUM(C3:C5)")
    checked = service.recheck(task_id, 1)
    assert checked.status == "failed" and any(
        i.code == "TOTAL_RANGE_INCOMPLETE" for i in checked.issues
    )


def test_upstream_new_plan_invalidates_delivery_and_downloads(tmp_path: Path) -> None:
    """人工新匹配版本必须重新制表，旧生成不能继续作为最新交付。

    Args:
        tmp_path: 隔离存储目录。
    """
    task_id, service, match_version = delivery_task(tmp_path)
    first = service.run(task_id, DeliveryRequest(expected_match_plan_version=match_version))
    matching = service.matching
    current = matching.get(task_id)
    candidate = next(
        c
        for row in current.progress
        for c in row.candidates
        if c.eligible and c.relation_type == "one_to_many"
    )
    new = matching.confirm(
        task_id,
        MatchConfirmation(
            actor="主管",
            reason="已重新确认当前对应关系",
            expected_match_plan_version=match_version,
            decision=ManualDecision(action="accept", candidate_id=candidate.candidate_id),
        ),
    )
    with pytest.raises(PreparationError, match="版本已变化"):
        service.artifact(task_id, "c_reconciled.xlsx", first.generation_version)
    second = service.run(task_id, DeliveryRequest(expected_match_plan_version=new.version))
    assert second.generation_version == 2 and second.match_plan_version == new.version


class BrokenOnceGenerator(CGenerator):
    """第一次制造写入错误，下一次恢复，验证修复确实重跑完整验收。"""

    def __init__(self) -> None:
        """初始化真实生成调用计数。"""
        self.calls: int = 0

    def generate(
        self, baseline_path: Path, inputs: DeliveryInputs, generation_version: int
    ) -> tuple[bytes, GenerationManifest]:
        """第一次模拟工具中断，第二次生成正确文件。

        Args:
            baseline_path: 不可变底稿路径。
            inputs: 冻结的来源和匹配关系。
            generation_version: 当前生成版本。

        Returns:
            第二次真实生成的文件和布局；第一次抛出模拟错误。
        """
        self.calls += 1
        if self.calls == 1:
            raise OSError("模拟瞬时文件工具失败")
        return super().generate(baseline_path, inputs, generation_version)


def test_bounded_repair_reexecutes_generation_and_verification(tmp_path: Path) -> None:
    """瞬时工具错误可在预算内修复；不会绕过验收或重复展开业务。

    Args:
        tmp_path: 隔离存储目录。
    """
    task_id, initial, match_version = delivery_task(tmp_path)
    generator = BrokenOnceGenerator()
    service = DeliveryService(
        Settings(storage_root=initial.repository.root, llm_model=None, llm_api_key=None),
        generator=generator,
    )
    state = service.run(task_id, DeliveryRequest(expected_match_plan_version=match_version))
    assert state.status == "completed_with_review" and state.attempt == generator.calls == 2
    assert any(e.stage == "repair_delivery" for e in state.events)
    assert service.result(task_id, 1).statistics.a_detail_count == 3


@pytest.mark.parametrize(
    ("a_quantities", "b_quantity", "expected_status", "remaining"),
    [
        ((2, 3), 5, "completed", "0"),
        ((3,), 5, "completed_with_review", "2"),
        ((2, 3), 7, "completed_with_review", "2"),
    ],
)
def test_many_to_one_and_partial_preserve_single_b_trace(
    tmp_path: Path,
    a_quantities: tuple[int, ...],
    b_quantity: int,
    expected_status: str,
    remaining: str,
) -> None:
    """共享 B 原值只展示一次，A 业务不丢失，部分余额归缺失而不重复计为承接。

    Args:
        tmp_path: 隔离存储目录。
        a_quantities: A 各业务数量。
        b_quantity: 唯一 B 原始数量。
        expected_status: 有无待复核事项的预期完成状态。
        remaining: B 的预期未结数量。
    """
    a_content, b_content = simple_books(a_quantities, b_quantity)
    task_id, service, version = delivery_task(tmp_path, a_content=a_content, b_content=b_content)
    if len(a_quantities) > 1 and remaining != "0":
        state = service.matching.get(task_id)
        c = next(
            c
            for row in state.progress
            for c in row.candidates
            if c.eligible and len(c.a_record_ids) == len(a_quantities)
        )
        version = service.matching.confirm(
            task_id,
            MatchConfirmation(
                actor="复核员",
                reason="确认统一部分共享组",
                expected_match_plan_version=version,
                decision=ManualDecision(action="accept", candidate_id=c.candidate_id),
            ),
        ).version
    generated = service.run(task_id, DeliveryRequest(expected_match_plan_version=version))
    assert generated.status == expected_status, [(i.code, i.message) for i in generated.issues]
    result = service.result(task_id, 1)
    report = service.report(task_id, 1)
    assert report.coverage[0].remaining_quantity == remaining
    assert result.statistics.b_current_month_business_records == 1
    assert (
        result.statistics.taken_business_records,
        result.statistics.missing_business_records,
    ) == ((1, 0) if remaining == "0" else (0, 1))
    worksheet = load_workbook(workbook_path(service, task_id, 1, "c_workbook"), data_only=True)[
        "业务底稿"
    ]
    marker_column = next(cell.column for cell in worksheet[2] if cell.value == "B来源标识")
    assert (
        sum(
            bool(worksheet.cell(row, marker_column).value)
            for row in range(3, worksheet.max_row + 1)
        )
        == 1
    )
    actual_quantities: list[int] = [
        int(str(worksheet.cell(row, 3).value or 0)) for row in range(3, len(a_quantities) + 3)
    ]
    assert sum(actual_quantities) == sum(a_quantities)
    assert report.passed


def test_confirmed_exclusion_is_a_separate_covered_disposition(tmp_path: Path) -> None:
    """重复来源分别核查，已确认排除有业务原因及确认引用，不算作缺失或承接。

    Args:
        tmp_path: 隔离存储目录。
    """
    task_id, service, version = delivery_task(tmp_path)
    plan = service.matching.get(task_id)
    duplicate = next(
        i for row in plan.progress for c in row.candidates for i in c.b_record_ids if ":5:" in i
    )
    state = service.matching.confirm(
        task_id,
        MatchConfirmation(
            actor="主管",
            reason="本条为已核实独立业务，按业务约定排除",
            expected_match_plan_version=version,
            decision=ManualDecision(action="exclude_b", b_record_ids=(duplicate,)),
        ),
    )
    generated = service.run(task_id, DeliveryRequest(expected_match_plan_version=state.version))
    assert generated.status == "completed_with_review"
    result = service.result(task_id, 1)
    assert (
        result.statistics.excluded_business_records == 1
        and result.statistics.missing_business_records == 2
    )
    excluded = next(r for r in service.report(task_id, 1).coverage if r.b_record_id == duplicate)
    assert (
        excluded.disposition == "excluded"
        and excluded.confirmation_ref
        and excluded.exclusion_reason
    )


def test_unsupported_formula_remains_not_run_and_blocks_final_delivery(tmp_path: Path) -> None:
    """未知公式不能只保留字符串或旧缓存后宣称已重算。

    Args:
        tmp_path: 隔离存储目录。
    """
    workbook = load_workbook(io.BytesIO(a_workbook()))
    workbook["原始附件"]["D1"] = "=IF(1=1,1,0)"
    buffer = io.BytesIO()
    workbook.save(buffer)
    task_id, service, version = delivery_task(tmp_path, a_content=buffer.getvalue())
    state = service.run(task_id, DeliveryRequest(expected_match_plan_version=version))
    assert state.status == "needs_input"
    assert any(i.code == "FORMULA_NOT_SUPPORTED" for i in state.issues)
    assert any(check.status == "not_run" for check in service.report(task_id, 1).checks)
    with pytest.raises(PreparationError):
        service.artifact(task_id, "c_reconciled.xlsx", 1)


def test_cross_month_trace_requires_explicit_range_and_is_reported(tmp_path: Path) -> None:
    """已确认日期范围内保留跨月追溯，但不把八月记录计入七月覆盖等式。

    Args:
        tmp_path: 隔离存储目录。
    """
    a_content, b_content = simple_books((3,), 3, a_day=31, b_month=8)
    task_id, service, version = delivery_task(tmp_path, a_content=a_content, b_content=b_content)
    updated = service.matching.confirm(
        task_id,
        MatchConfirmation(
            actor="主管",
            reason="已确认系统跨日入库可偏移一天",
            expected_match_plan_version=version,
            decision=ManualDecision(
                action="rules", rules=MatchingRules(version="cross-month-v1", date_tolerance_days=1)
            ),
        ),
    )
    state = service.run(task_id, DeliveryRequest(expected_match_plan_version=updated.version))
    assert state.status == "completed_with_review"
    report = service.report(task_id, 1)
    assert (
        report.statistics.b_current_month_business_records == 0
        and report.statistics.outside_period_business_records == 1
    )
    assert report.coverage[0].c_references and "跨月追溯" in report.coverage[0].explanation


def test_missing_registered_workbook_revokes_completion(tmp_path: Path) -> None:
    """文件丢失的复验不能报 500 后继续保留正式完成资格。

    Args:
        tmp_path: 隔离存储目录。
    """
    task_id, service, version = delivery_task(tmp_path)
    service.run(task_id, DeliveryRequest(expected_match_plan_version=version))
    path = workbook_path(service, task_id, 1, "c_workbook")
    path.unlink()
    state = service.recheck(task_id, 1)
    assert state.status == "failed" and state.result_ref is None
    assert any(i.code == "OUTPUT_UNREADABLE" for i in state.issues)
    assert service.artifact(task_id, "execution_diagnostic.json", 1, diagnostic=True).is_file()
    restored = service.run(task_id, DeliveryRequest(expected_match_plan_version=version))
    assert restored.generation_version == 2 and restored.status == "completed_with_review"


def test_source_precision_beyond_display_capability_is_preserved_but_not_certified(
    tmp_path: Path,
) -> None:
    """超出当前显示能力的源值完整保留，不靠截断为两位数换取验收通过。

    Args:
        tmp_path: 隔离存储目录。
    """
    precise = "15.769826123456789012345"
    source = replace_cell_xml(b_workbook(), "L2", value=precise)
    task_id, service, version = delivery_task(tmp_path, b_content=source)
    state = service.run(task_id, DeliveryRequest(expected_match_plan_version=version))
    assert state.status == "needs_input"
    assert any(i.code == "DISPLAY_PRECISION_UNVERIFIED" for i in state.issues)
    diagnostic = service.artifact(task_id, "b_annotated.xlsx", 1, diagnostic=True)
    with ZipFile(diagnostic) as archive:
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    cell = next(c for c in root.iter(tag("c")) if c.get("r") == "L2")
    assert cell.findtext(tag("v")) == precise
