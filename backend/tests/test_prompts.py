"""提示词变量契约、模型消息接线和配置失败语义的离线验证。"""

import json
from pathlib import Path

import pytest
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import SecretStr

from app.core.config import Settings
from app.preparation.application.service import PreparationService
from app.preparation.domain.models import (
    FileRole,
    MappingAdvice,
    SheetProfile,
    TableProfiles,
    TaskOptions,
    TaskStatus,
)
from app.preparation.infrastructure import llm as llm_module
from app.preparation.prompts import base as prompt_base
from app.preparation.prompts.base import (
    PromptConfigurationError,
    PromptDefinition,
    render_prompt,
)
from app.preparation.prompts.field_mapping import build_field_mapping_prompt
from tests.workbooks import a_workbook, b_workbook


def sample_profiles() -> TableProfiles:
    """构建包含美元符号及伪占位符的业务样例。

    Returns:
        用于验证内容只插入一次的原始结构摘要。
    """
    return TableProfiles(
        sheets=(
            SheetProfile(
                role=FileRole.A,
                name="sentinel_${profiles_json}",
                max_row=10,
                merged_ranges=("A2:A3",),
                sample_rows={"2": {"A": "7月1日", "B": '${not_a_variable} $5 {"原始备注":true}'}},
            ),
        )
    )


def install_test_templates(
    monkeypatch: pytest.MonkeyPatch, directory: Path, *, user_text: str
) -> PromptDefinition:
    """注入隔离文案目录，测试资源和模板错误而不修改生产文案。

    Args:
        monkeypatch: 自动恢复补丁的 pytest 工具。
        directory: 本测试专用目录。
        user_text: 要验证的用户模板。

    Returns:
        指向测试资源名称的合法定义。
    """
    templates = directory / "templates"
    templates.mkdir()
    (templates / "system.md").write_text("识别结构，保留原始证据。", encoding="utf-8")
    (templates / "user.md").write_text(user_text, encoding="utf-8")

    def package_files(package: str) -> Path:
        """替换包资源定位。

        Args:
            package: 生产加载器请求的包标识。

        Returns:
            测试专用的资源根目录。
        """
        assert package == "app.preparation.prompts"
        return directory

    monkeypatch.setattr(prompt_base, "files", package_files)
    return PromptDefinition("test.mapping", "1.0.0", "system.md", "user.md")


def test_real_prompt_preserves_json_without_recursive_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """在其他启动目录验证包资源定位及原始输入保真。

    Args:
        tmp_path: 模拟与 backend 不同的启动目录。
        monkeypatch: 测试结束后恢复启动目录。
    """
    profiles = sample_profiles()
    monkeypatch.chdir(tmp_path)
    prompt = build_field_mapping_prompt(profiles)
    assert profiles.model_dump_json(indent=2) in prompt.user_message
    assert "${not_a_variable}" in prompt.user_message
    assert prompt.system_message and prompt.prompt_id == "preparation.field_mapping"
    assert "sentinel_" not in json.dumps(prompt.trace_metadata())
    other = build_field_mapping_prompt(TableProfiles(sheets=()))
    assert prompt.template_sha256 == other.template_sha256


def test_template_edit_changes_fingerprint_without_caching_old_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证下一次调用读取新文案，并产生新的模板摘要。

    Args:
        tmp_path: 文案测试目录。
        monkeypatch: 包资源定位替身。
    """
    definition = install_test_templates(monkeypatch, tmp_path, user_text="输入：${data}")
    before = render_prompt(definition, {"data": "业务输入"})
    (tmp_path / "templates/user.md").write_text("新文案：${data}", encoding="utf-8")
    after = render_prompt(definition, {"data": "业务输入"})
    assert before.template_sha256 != after.template_sha256
    assert before.user_message != after.user_message


@pytest.mark.parametrize("user_text", ["${missing}", "${data} ${missing}", "${data} $", ""])
def test_invalid_template_cannot_silently_drop_or_skip_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user_text: str
) -> None:
    """验证变量不匹配、语法错误或空资源都会明确失败。

    Args:
        tmp_path: 文案测试目录。
        monkeypatch: 包资源定位替身。
        user_text: 有意损坏的模板文本。
    """
    definition = install_test_templates(monkeypatch, tmp_path, user_text=user_text)
    with pytest.raises(PromptConfigurationError) as captured:
        render_prompt(definition, {"data": "业务敏感内容不得出现在配置错误中"})
    assert captured.value.code == "PROMPT_CONFIGURATION_ERROR"
    assert "业务敏感内容" not in captured.value.message


def test_missing_template_is_a_locatable_configuration_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证部署缺失文案时不会空提示继续调用模型。

    Args:
        tmp_path: 模拟不完整部署目录。
        monkeypatch: 包资源定位替身。
    """
    definition = install_test_templates(monkeypatch, tmp_path, user_text="${data}")
    (tmp_path / "templates/user.md").unlink()
    with pytest.raises(PromptConfigurationError, match="user.md"):
        render_prompt(definition, {"data": "输入"})


def test_model_adapter_uses_module_messages_metadata_and_existing_output_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证实际适配器通过独立构建器发消息，并继续使用原输出协议。

    Args:
        monkeypatch: 替换客户端，确保测试不调用网络。
    """
    calls: list[tuple[list[BaseMessage], RunnableConfig | None]] = []
    expected = MappingAdvice(mappings=(), explanation="缺少确定性依据")

    class RecordingClient:
        """记录真实模型调用边界。"""

        def invoke(
            self, messages: list[BaseMessage], *, config: RunnableConfig | None = None
        ) -> MappingAdvice:
            """记录消息及配置并返回类型化结果。

            Args:
                messages: 模型适配器构建的消息。
                config: 本次运行名称及元数据。

            Returns:
                原有 MappingAdvice 协议的测试结果。
            """
            calls.append((messages, config))
            return expected

    class FakeChatModel:
        """只替换供应商调用，不替换被测试的提示词构建。"""

        def with_structured_output(self, schema: type[MappingAdvice]) -> RecordingClient:
            """验证结构化输出协议并返回离线客户端。

            Args:
                schema: 实际适配器提交的输出模型。

            Returns:
                记录消息的客户端。
            """
            assert schema is MappingAdvice
            return RecordingClient()

    def client_factory(**kwargs: object) -> FakeChatModel:
        """构造离线模型替身。

        Args:
            kwargs: 实际模型配置，测试不输出其值。

        Returns:
            不产生网络请求的模型适配对象。
        """
        assert kwargs["model"] == "offline-model"
        return FakeChatModel()

    monkeypatch.setattr(llm_module, "ChatOpenAI", client_factory)
    advisor = llm_module.StructuredMappingAdvisor(
        Settings(llm_model="offline-model", llm_api_key=SecretStr("offline-key"))
    )
    profiles = sample_profiles()
    assert advisor.suggest(profiles) is expected
    messages, config = calls[0]
    prompt = build_field_mapping_prompt(profiles)
    assert len(messages) == 2
    assert isinstance(messages[0], SystemMessage) and isinstance(messages[1], HumanMessage)
    assert messages[0].content == prompt.system_message
    assert messages[1].content == prompt.user_message
    assert config is not None and config["run_name"] == prompt.prompt_id
    assert config["metadata"] == prompt.trace_metadata()


def test_prompt_configuration_failure_is_not_misreported_as_missing_user_mapping(
    tmp_path: Path,
) -> None:
    """验证文案损坏时明确标记配置失败，不要求用户反复补业务映射。

    Args:
        tmp_path: 本测试的独立任务存储目录。
    """

    class BrokenPromptAdvisor:
        """模拟文案部署错误。"""

        def suggest(self, profiles: TableProfiles) -> MappingAdvice:
            """读取 profiles 后模拟配置失败；本方法不会产生模型结果。"""
            raise PromptConfigurationError("preparation.field_mapping", "文案资源缺失")

    service = PreparationService(
        Settings(storage_root=tmp_path, llm_model=None, llm_api_key=None),
        advisor=BrokenPromptAdvisor(),
    )
    task = service.create(
        "a.xlsx",
        a_workbook(unknown_headers=True),
        "b.xlsx",
        b_workbook(),
        TaskOptions(reconciliation_period="2026-07"),
    )
    result = service.run(task.task_id)
    assert result.status == TaskStatus.FAILED
    assert result.issues[0].code == "PROMPT_CONFIGURATION_ERROR"
