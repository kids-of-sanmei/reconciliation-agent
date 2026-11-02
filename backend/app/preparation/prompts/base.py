"""独立于模型供应商的提示词资源加载、严格渲染和版本标识。"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import PurePosixPath
from string import Template

from app.preparation.domain.errors import PreparationError


class PromptConfigurationError(PreparationError):
    """提示词资源或变量契约损坏，应修复配置后重试。"""

    def __init__(self, prompt_id: str, reason: str) -> None:
        """构造可定位、但不包含业务输入的配置错误。

        Args:
            prompt_id: 出错提示词的稳定标识。
            reason: 资源或变量契约失败的具体原因。
        """
        super().__init__("PROMPT_CONFIGURATION_ERROR", f"提示词 {prompt_id} 配置错误：{reason}")


@dataclass(frozen=True, slots=True)
class PromptDefinition:
    """固定文案资源与版本，资源路径只能指向本模块 templates 下的 Markdown。"""

    prompt_id: str
    version: str
    system_resource: str
    user_resource: str

    def __post_init__(self) -> None:
        """检查提示词标识、版本和资源名称，拒绝无效定义及路径跳转。"""
        if not self.prompt_id.strip() or re.fullmatch(r"\d+\.\d+\.\d+", self.version) is None:
            raise PromptConfigurationError(self.prompt_id, "需要非空标识及 major.minor.patch 版本")
        for resource in (self.system_resource, self.user_resource):
            if (
                PurePosixPath(resource).name != resource
                or "\\" in resource
                or not resource.endswith(".md")
            ):
                raise PromptConfigurationError(
                    self.prompt_id, "资源必须是 templates 下的 Markdown 文件名"
                )


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    """装配后的供应商无关消息，以及不包含业务输入的追踪信息。"""

    prompt_id: str
    version: str
    template_sha256: str
    system_message: str
    user_message: str

    def trace_metadata(self) -> dict[str, str]:
        """构建模型调用的追踪元数据。

        Returns:
            提示词标识、版本和模板内容摘要，不含工作表样例或密钥。
        """
        return {
            "prompt_id": self.prompt_id,
            "prompt_version": self.version,
            "prompt_template_sha256": self.template_sha256,
        }


def _read_template(definition: PromptDefinition, resource: str) -> str:
    """按包资源路径读取文案，避免依赖进程当前目录。

    Args:
        definition: 用于定位错误的提示词定义。
        resource: 已验证的 templates 内资源文件名。

    Returns:
        去除首尾空白的非空 UTF-8 模板。
    """
    try:
        content = (
            files("app.preparation.prompts")
            .joinpath("templates", resource)
            .read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError) as exc:
        raise PromptConfigurationError(
            definition.prompt_id, f"无法读取文案资源 {resource}"
        ) from exc
    content = content.strip()
    if not content:
        raise PromptConfigurationError(definition.prompt_id, f"文案资源 {resource} 为空")
    return content


def render_prompt(definition: PromptDefinition, variables: Mapping[str, str]) -> RenderedPrompt:
    """严格装配系统和用户提示词，插入值只替换一次。

    Args:
        definition: 提示词标识、版本及两个文案资源名称。
        variables: 模板需要的完整变量集合，值必须已序列化为文本。

    Returns:
        可直接转换为模型消息的提示词及模板版本、内容摘要。

    Raises:
        PromptConfigurationError: 文案不存在、为空、语法错误或变量不匹配。
    """
    system_text = _read_template(definition, definition.system_resource)
    user_text = _read_template(definition, definition.user_resource)
    templates = (Template(system_text), Template(user_text))
    if not all(template.is_valid() for template in templates):
        raise PromptConfigurationError(definition.prompt_id, "模板包含无效的 $ 占位符")
    expected = {name for template in templates for name in template.get_identifiers()}
    provided = set(variables)
    if expected != provided:
        missing = ", ".join(sorted(expected - provided)) or "无"
        unused = ", ".join(sorted(provided - expected)) or "无"
        raise PromptConfigurationError(
            definition.prompt_id, f"缺少变量：{missing}；未使用变量：{unused}"
        )
    fingerprint = json.dumps(
        {
            "prompt_id": definition.prompt_id,
            "version": definition.version,
            "system": system_text,
            "user": user_text,
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return RenderedPrompt(
        prompt_id=definition.prompt_id,
        version=definition.version,
        template_sha256=hashlib.sha256(fingerprint).hexdigest(),
        system_message=templates[0].substitute(variables),
        user_message=templates[1].substitute(variables),
    )
