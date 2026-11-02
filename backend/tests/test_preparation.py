"""第一块业务链路、来源精度和持久化恢复的回归测试。"""

import hashlib
import io
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from openpyxl import load_workbook

from app.core.config import Settings
from app.preparation.application.mapping import detect_mappings
from app.preparation.application.normalization import parse_business_date, parse_decimal
from app.preparation.application.service import PreparationService
from app.preparation.domain.errors import PreparationError
from app.preparation.domain.models import (
    Confirmation,
    Direction,
    FileRole,
    LookupIndex,
    MappingAdvice,
    RecordSet,
    SheetMapping,
    SourceSnapshots,
    TableProfiles,
    TaskOptions,
    TaskStatus,
)
from tests.workbooks import MAIN_NS, a_workbook, b_workbook, replace_cell_xml


def service_at(path: Path) -> PreparationService:
    """创建不调用真实模型的隔离测试服务。

    Args:
        path: 本测试专用的持久化目录。

    Returns:
        使用真实 LangGraph 和工作簿适配器的服务。
    """
    return PreparationService(Settings(storage_root=path, llm_model=None, llm_api_key=None))


def test_complete_preparation_preserves_sources_precision_and_layout(tmp_path: Path) -> None:
    """验证完整准备、合并日期、来源字节、附件表、样式和精确数字；tmp_path 为隔离目录。"""
    service = service_at(tmp_path)
    original = replace_cell_xml(a_workbook(), "D3", value="15.769826123456789")
    task = service.create(
        "业务底稿.xlsx",
        original,
        "台州系统.xlsx",
        b_workbook(),
        TaskOptions(reconciliation_period="2026-07"),
    )
    result = service.run(task.task_id)
    assert result.status == TaskStatus.READY, result.issues
    prepared = service.prepared(task.task_id)
    assert prepared.a_baseline_totals[0].quantity == "459"
    assert prepared.a_baseline_totals[0].amount == "7235.511612"
    a_data = service.repository.read_model(
        task.task_id, prepared.a_records_ref.relative_path, RecordSet
    )
    assert len(a_data.records) == 3
    assert a_data.records[1].normalized_fields.date == "2026-07-01"
    assert a_data.records[1].normalized_fields.quantity == "0"
    assert a_data.records[2].direction == Direction.OUT
    assert a_data.records[0].normalized_fields.unit_price == "15.769826123456789"
    baseline_path = service.artifact(task.task_id, "c_baseline.xlsx")
    baseline = load_workbook(baseline_path)
    sheet = baseline["业务底稿"]
    assert sheet["A3"].value == sheet["A4"].value == "7月1日"
    assert not sheet.merged_cells.ranges
    assert sheet["B3"].fill.fill_type is None
    assert sheet["B3"].font.bold is True
    assert sheet["B3"].border.bottom is not None
    assert sheet["B3"].border.bottom.style == "thin"
    assert sheet["D3"].number_format == sheet["E3"].number_format == "General"
    assert sheet.row_dimensions[4].hidden
    assert sheet.column_dimensions["B"].width == 28
    assert baseline["原始附件"]["A1"].value == "完整保留此工作表"
    baseline.close()
    with ZipFile(io.BytesIO(original)) as before, ZipFile(baseline_path) as after:
        assert before.namelist() == after.namelist()
        for name in before.namelist():
            if name not in {"xl/worksheets/sheet1.xml", "xl/styles.xml"}:
                assert before.read(name) == after.read(name), name
        root = ET.fromstring(after.read("xl/worksheets/sheet1.xml"))
        cell = next(node for node in root.iter(f"{{{MAIN_NS}}}c") if node.get("r") == "D3")
        value = cell.find(f"{{{MAIN_NS}}}v")
        assert value is not None and value.text == "15.769826123456789"
    original_path = service.repository.path(task.task_id, "sources/input_a.xlsx")
    assert original_path.read_bytes() == original
    assert hashlib.sha256(original).hexdigest() == task.sources[0].sha256
    assert service.run(task.task_id).data_version == 1


def test_b_directions_duplicates_zero_and_month_scope(tmp_path: Path) -> None:
    """验证 B 双向、相同内容不同身份、零值和跨月均被保留；tmp_path 为隔离目录。"""
    service = service_at(tmp_path)
    task = service.create(
        "a.xlsx", a_workbook(), "b.xlsx", b_workbook(), TaskOptions(reconciliation_period="2026-07")
    )
    assert service.run(task.task_id).status == TaskStatus.READY
    prepared = service.prepared(task.task_id)
    data = service.repository.read_model(
        task.task_id, prepared.b_records_ref.relative_path, RecordSet
    )
    assert len(data.records) == 7
    assert sum(record.direction == Direction.OUT for record in data.records) == 2
    assert sum(record.direction == Direction.UNKNOWN for record in data.records) == 1
    assert sum(not record.in_period for record in data.records) == 1
    duplicates = [record for record in data.records if record.normalized_fields.quantity == "449"]
    assert len(duplicates) == 2 and duplicates[0].record_id != duplicates[1].record_id
    assert duplicates[0].normalized_fields.fixed_cost == "16.716"
    index = service.repository.read_model(
        task.task_id, prepared.b_index_ref.relative_path, LookupIndex
    )
    assert len(index.by_quantity["449"]) == 2
    assert "2026-08-01" in index.by_date
    assert len(index.by_quantity["0"]) == 1


def test_missing_period_resumes_after_service_restart(tmp_path: Path) -> None:
    """验证 SQLite 中断在服务重建后继续执行；tmp_path 为持久化目录。"""
    first = service_at(tmp_path)
    task = first.create("a.xlsx", a_workbook(), "b.xlsx", b_workbook(), TaskOptions())
    waiting = first.run(task.task_id)
    assert waiting.status == TaskStatus.NEEDS_INPUT
    assert any(issue.code == "PERIOD_REQUIRED" for issue in waiting.issues)
    with pytest.raises(PreparationError, match="尚未完成"):
        first.prepared(task.task_id)
    restarted = service_at(tmp_path)
    result = restarted.run(
        task.task_id,
        Confirmation(
            actor="财务复核人",
            reason="确认业务月份",
            options=TaskOptions(reconciliation_period="2026-07"),
        ),
    )
    assert result.status == TaskStatus.READY, result.issues
    assert result.data_version == 2
    assert len(result.confirmations) == 1
    assert (
        restarted.prepared(task.task_id).task_context.confirmations[0].confirmation.actor
        == "财务复核人"
    )


def test_unknown_headers_accept_explicit_validated_mapping(tmp_path: Path) -> None:
    """验证陌生表头可补充字段后续跑；tmp_path 为隔离目录。"""
    service = service_at(tmp_path)
    task = service.create(
        "a.xlsx",
        a_workbook(unknown_headers=True),
        "b.xlsx",
        b_workbook(),
        TaskOptions(reconciliation_period="2026-07"),
    )
    assert service.run(task.task_id).status == TaskStatus.NEEDS_INPUT
    snapshots = service.repository.read_model(
        task.task_id, "sources/snapshot.json", SourceSnapshots
    )
    a_source = next(item for item in snapshots.workbooks if item.source_file.role == FileRole.A)
    normal_source = service.steps.workbook.read(
        service.repository.path(task.task_id, "sources/input_b.xlsx"), task.sources[1]
    )
    mapping = SheetMapping.model_validate_json(
        '{"role":"a","sheet_name":"业务底稿","header_row":2,"data_start_row":3,"data_end_row":6,"fields":{"date":"A","spec":"B","quantity":"C","unit_price":"D","amount":"E"}}'
    )
    assert a_source.sheets[0].name == mapping.sheet_name
    options = TaskOptions(
        reconciliation_period="2026-07", mappings=(mapping,) + detect_mappings(normal_source)
    )
    result = service.run(
        task.task_id, Confirmation(actor="财务", reason="说明陌生字段", options=options)
    )
    assert result.status == TaskStatus.READY, result.issues


def test_missing_formula_cache_requires_confirmed_decimal(tmp_path: Path) -> None:
    """验证金额公式不能当零，人员确认后落为精确数值；tmp_path 为隔离目录。"""
    service = service_at(tmp_path)
    task = service.create(
        "a.xlsx",
        a_workbook(formula=True),
        "b.xlsx",
        b_workbook(),
        TaskOptions(reconciliation_period="2026-07"),
    )
    waiting = service.run(task.task_id)
    assert waiting.status == TaskStatus.NEEDS_INPUT
    formula_issue = next(
        issue for issue in waiting.issues if issue.code == "FORMULA_RESULT_REQUIRED"
    )
    assert formula_issue.source_ref is not None
    with pytest.raises(PreparationError, match="真实的来源公式"):
        service.run(
            task.task_id,
            Confirmation(actor="财务", reason="错误来源", formula_values={"not:a:formula": "1"}),
        )
    assert service.get(task.task_id).data_version == 1
    result = service.run(
        task.task_id,
        Confirmation(
            actor="财务",
            reason="核实公式计算结果",
            formula_values={formula_issue.source_ref: "7285.659612"},
        ),
    )
    assert result.status == TaskStatus.READY, result.issues
    baseline = load_workbook(service.artifact(task.task_id, "c_baseline.xlsx"), data_only=False)
    assert baseline["业务底稿"]["E3"].data_type == "n"
    assert baseline["业务底稿"]["E3"].value == 7285.659612
    baseline.close()


def test_existing_formula_cache_is_materialized_without_model(tmp_path: Path) -> None:
    """验证来源已有公式缓存可直接承接；tmp_path 为隔离目录。"""
    service = service_at(tmp_path)
    content = replace_cell_xml(a_workbook(formula=True), "E3", value="7285.659612", cached=True)
    task = service.create(
        "a.xlsx", content, "b.xlsx", b_workbook(), TaskOptions(reconciliation_period="2026-07")
    )
    assert service.run(task.task_id).status == TaskStatus.READY


def test_1904_epoch_dates_are_not_shifted(tmp_path: Path) -> None:
    """验证 1904 日期系统不发生多年偏移；tmp_path 为隔离目录。"""
    service = service_at(tmp_path)
    task = service.create(
        "a.xlsx",
        a_workbook(epoch_1904=True),
        "b.xlsx",
        b_workbook(),
        TaskOptions(reconciliation_period="2026-07"),
    )
    assert service.run(task.task_id).status == TaskStatus.READY
    prepared = service.prepared(task.task_id)
    data = service.repository.read_model(
        task.task_id, prepared.a_records_ref.relative_path, RecordSet
    )
    assert data.records[0].normalized_fields.date == "2026-07-01"


def test_invalid_workbook_is_a_persisted_failure(tmp_path: Path) -> None:
    """验证损坏文件给出失败任务而非虚假就绪；tmp_path 为隔离目录。"""
    service = service_at(tmp_path)
    task = service.create(
        "a.xlsx", b"broken", "b.xlsx", b_workbook(), TaskOptions(reconciliation_period="2026-07")
    )
    result = service.run(task.task_id)
    assert result.status == TaskStatus.FAILED
    assert result.issues[0].code == "INVALID_WORKBOOK"


def test_cross_process_execution_lock_prevents_duplicate_run(tmp_path: Path) -> None:
    """验证独立锁实例无法同时执行同一任务；tmp_path 为隔离目录。"""
    service = service_at(tmp_path)
    task = service.create(
        "a.xlsx", a_workbook(), "b.xlsx", b_workbook(), TaskOptions(reconciliation_period="2026-07")
    )
    with service.repository.lock(task.task_id), pytest.raises(PreparationError, match="正在执行"):
        service_at(tmp_path).run(task.task_id)
    assert service.run(task.task_id).status == TaskStatus.READY


def test_invalid_mapping_from_model_is_not_trusted(tmp_path: Path) -> None:
    """验证模型生成的越界字段必须经过程序拒绝；tmp_path 为隔离目录。"""

    class InvalidAdvisor:
        """故意给出不存在工作表的测试替身。"""

        def suggest(self, profiles: TableProfiles) -> MappingAdvice:
            """接收 profiles 结构摘要，返回有问题的映射以验证边界。"""
            return MappingAdvice.model_validate_json(
                '{"mappings":[{"role":"a","sheet_name":"不存在","header_row":1,"data_start_row":2,"data_end_row":5,"fields":{"date":"A"}}],"explanation":"测试"}'
            )

    service = PreparationService(
        Settings(storage_root=tmp_path, llm_model=None), advisor=InvalidAdvisor()
    )
    task = service.create(
        "a.xlsx",
        a_workbook(unknown_headers=True),
        "b.xlsx",
        b_workbook(),
        TaskOptions(reconciliation_period="2026-07"),
    )
    result = service.run(task.task_id)
    assert result.status == TaskStatus.NEEDS_INPUT
    assert any(issue.code == "UNKNOWN_SHEET" for issue in result.issues)
    titles = [event.title for event in result.events]
    assert "调用模型辅助识别字段" in titles
    assert "模型已返回字段建议" in titles
    assert result.events[-1].kind == "waiting"
    assert "字段及对账月份校验通过" not in titles


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1,2", "", "abc"])
def test_invalid_numbers_are_not_coerced_to_zero(value: str) -> None:
    """value 为非法来源数字，预期明确报错而非返回零。"""
    with pytest.raises(ValueError):
        parse_decimal(value)


def test_date_and_decimal_parsing_retains_business_meaning() -> None:
    """验证业务日期、千分位和负数的精确解析。"""
    assert str(parse_decimal("(1,234.56789)")) == "-1234.56789"
    assert parse_business_date("2026.7.1", "2026-07", "1900").isoformat() == "2026-07-01"
    assert parse_business_date("7月1日", "2026-07", "1900").isoformat() == "2026-07-01"
    with pytest.raises(ValueError):
        parse_business_date("60", "2026-07", "1900")


def test_source_change_during_confirmation_cannot_publish(tmp_path: Path) -> None:
    """验证等待确认期间来源变化会阻止发布；tmp_path 为隔离目录。"""
    service = service_at(tmp_path)
    task = service.create("a.xlsx", a_workbook(), "b.xlsx", b_workbook(), TaskOptions())
    assert service.run(task.task_id).status == TaskStatus.NEEDS_INPUT
    source = service.repository.path(task.task_id, "sources/input_b.xlsx")
    source.write_bytes(source.read_bytes() + b"changed")
    result = service.run(
        task.task_id,
        Confirmation(
            actor="财务", reason="确认月份", options=TaskOptions(reconciliation_period="2026-07")
        ),
    )
    assert result.status == TaskStatus.FAILED
    assert result.issues[0].code == "SOURCE_CHANGED"


def test_confirmation_replay_is_idempotent_and_timestamped(tmp_path: Path) -> None:
    """验证中断重放不会重复增加版本，审计保留时间；tmp_path 为隔离目录。"""
    service = service_at(tmp_path)
    task = service.create("a.xlsx", a_workbook(), "b.xlsx", b_workbook(), TaskOptions())
    assert service.run(task.task_id).status == TaskStatus.NEEDS_INPUT
    confirmation = Confirmation(
        actor="财务", reason="确认月份", options=TaskOptions(reconciliation_period="2026-07")
    )
    service.steps.confirm(task.task_id, confirmation)
    service.steps.confirm(task.task_id, confirmation)
    state = service.get(task.task_id)
    assert state.data_version == 2 and len(state.confirmations) == 1
    assert state.confirmations[0].confirmed_at.tzinfo is not None
    assert state.confirmations[0].input_data_version == 1


def test_stale_confirmation_does_not_consume_checkpoint(tmp_path: Path) -> None:
    """验证过期版本确认会在消费检查点前拒绝；tmp_path 为隔离目录。"""
    service = service_at(tmp_path)
    task = service.create("a.xlsx", a_workbook(), "b.xlsx", b_workbook(), TaskOptions())
    assert service.run(task.task_id).status == TaskStatus.NEEDS_INPUT
    with pytest.raises(PreparationError, match="版本已过期"):
        service.run(
            task.task_id, Confirmation(actor="财务", reason="旧请求", expected_data_version=99)
        )
    assert (
        service.run(
            task.task_id,
            Confirmation(
                actor="财务",
                reason="当前版本",
                expected_data_version=1,
                options=TaskOptions(reconciliation_period="2026-07"),
            ),
        ).status
        == TaskStatus.READY
    )


def test_optional_reference_is_preserved_without_requiring_mapping(tmp_path: Path) -> None:
    """验证人工参考表完整保存且不当作必需匹配表；tmp_path 为隔离目录。"""
    service = service_at(tmp_path)
    reference = a_workbook()
    task = service.create(
        "a.xlsx",
        a_workbook(),
        "b.xlsx",
        b_workbook(),
        TaskOptions(reconciliation_period="2026-07"),
        reference_name="人工调整.xlsx",
        reference_content=reference,
    )
    assert service.run(task.task_id).status == TaskStatus.READY
    prepared = service.prepared(task.task_id)
    assert len(prepared.task_context.files) == 3
    reference_source = next(
        item for item in prepared.task_context.files if item.role == FileRole.REFERENCE
    )
    assert (
        service.repository.path(task.task_id, reference_source.relative_path).read_bytes()
        == reference
    )


def test_date_is_not_filled_outside_original_merge(tmp_path: Path) -> None:
    """验证无合并依据的空日期不会继承上一行；tmp_path 为隔离目录。"""
    workbook = load_workbook(io.BytesIO(a_workbook()))
    workbook["业务底稿"].unmerge_cells("A3:A4")
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    service = service_at(tmp_path)
    task = service.create(
        "a.xlsx",
        buffer.getvalue(),
        "b.xlsx",
        b_workbook(),
        TaskOptions(reconciliation_period="2026-07"),
    )
    result = service.run(task.task_id)
    assert result.status == TaskStatus.NEEDS_INPUT
    assert any(
        issue.code == "INVALID_DATE"
        and issue.source_ref is not None
        and issue.source_ref.endswith(":A4")
        for issue in result.issues
    )


def test_business_name_equal_to_total_label_is_not_dropped(tmp_path: Path) -> None:
    """验证仓库名“合计”且日期有效时仍是业务行；tmp_path 为隔离目录。"""
    workbook = load_workbook(io.BytesIO(b_workbook()))
    workbook["系统出入库"]["E2"] = "合计"
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    service = service_at(tmp_path)
    task = service.create(
        "a.xlsx",
        a_workbook(),
        "b.xlsx",
        buffer.getvalue(),
        TaskOptions(reconciliation_period="2026-07"),
    )
    assert service.run(task.task_id).status == TaskStatus.READY
    prepared = service.prepared(task.task_id)
    records = service.repository.read_model(
        task.task_id, prepared.b_records_ref.relative_path, RecordSet
    )
    assert len(records.records) == 7
    assert records.records[0].normalized_fields.warehouse == "合计"


def test_materialized_formulas_invalidate_existing_excel_calculation_chain(tmp_path: Path) -> None:
    """验证真实 Excel 计算链不会引用已转为数值的公式单元格；tmp_path 为隔离目录。"""
    content = replace_cell_xml(a_workbook(formula=True), "E3", value="7285.659612", cached=True)
    buffer = io.BytesIO()
    package_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    content_ns = "http://schemas.openxmlformats.org/package/2006/content-types"
    with ZipFile(io.BytesIO(content)) as source, ZipFile(buffer, "w") as output:
        for info in source.infolist():
            data = source.read(info)
            if info.filename == "xl/_rels/workbook.xml.rels":
                root = ET.fromstring(data)
                ET.SubElement(
                    root,
                    f"{{{package_ns}}}Relationship",
                    {
                        "Id": "rIdChain",
                        "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/calcChain",
                        "Target": "calcChain.xml",
                    },
                )
                data = ET.tostring(root, encoding="utf-8")
            elif info.filename == "[Content_Types].xml":
                root = ET.fromstring(data)
                ET.SubElement(
                    root,
                    f"{{{content_ns}}}Override",
                    {
                        "PartName": "/xl/calcChain.xml",
                        "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.calcChain+xml",
                    },
                )
                data = ET.tostring(root, encoding="utf-8")
            output.writestr(info, data)
        output.writestr(
            "xl/calcChain.xml", f'<calcChain xmlns="{MAIN_NS}"><c r="E3" i="1"/></calcChain>'
        )
    service = service_at(tmp_path)
    task = service.create(
        "a.xlsx",
        buffer.getvalue(),
        "b.xlsx",
        b_workbook(),
        TaskOptions(reconciliation_period="2026-07"),
    )
    assert service.run(task.task_id).status == TaskStatus.READY
    with ZipFile(service.artifact(task.task_id, "c_baseline.xlsx")) as output:
        assert "xl/calcChain.xml" not in output.namelist()
        relationships = ET.fromstring(output.read("xl/_rels/workbook.xml.rels"))
        assert not any(node.get("Type", "").endswith("/calcChain") for node in relationships)
        assert b"/xl/calcChain.xml" not in output.read("[Content_Types].xml")
        workbook_root = ET.fromstring(output.read("xl/workbook.xml"))
        calculation = workbook_root.find(f"{{{MAIN_NS}}}calcPr")
        assert calculation is not None and calculation.get("fullCalcOnLoad") == "1"


def test_valid_model_advice_is_validated_and_persisted(tmp_path: Path) -> None:
    """验证可替换模型适配器能完成陌生字段识别并留下建议；tmp_path 为隔离目录。"""

    class ValidAdvisor:
        """模拟语义识别，不调用网络。"""

        def suggest(self, profiles: TableProfiles) -> MappingAdvice:
            """读取 profiles 结构摘要，返回符合源数据的 A 映射。"""
            assert len(profiles.sheets) == 3
            mapping = SheetMapping.model_validate_json(
                '{"role":"a","sheet_name":"业务底稿","header_row":2,"data_start_row":3,"data_end_row":6,"fields":{"date":"A","spec":"B","quantity":"C","unit_price":"D","amount":"E"}}'
            )
            return MappingAdvice(mappings=(mapping,), explanation="根据字段位置及数值样例识别")

    service = PreparationService(
        Settings(storage_root=tmp_path, llm_model=None), advisor=ValidAdvisor()
    )
    task = service.create(
        "a.xlsx",
        a_workbook(unknown_headers=True),
        "b.xlsx",
        b_workbook(),
        TaskOptions(reconciliation_period="2026-07"),
    )
    result = service.run(task.task_id)
    assert result.status == TaskStatus.READY
    titles = [event.title for event in result.events]
    assert titles.index("调用模型辅助识别字段") < titles.index("模型已返回字段建议")
    assert titles.index("模型已返回字段建议") < titles.index("字段及对账月份校验通过")
    advice = service.repository.read_model(
        task.task_id, "versions/1/mapping_advice.json", MappingAdvice
    )
    assert advice.mappings[0].sheet_name == "业务底稿"
