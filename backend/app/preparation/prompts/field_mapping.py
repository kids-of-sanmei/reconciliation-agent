"""第一块表结构识别提示词的定义与输入装配入口。"""

from app.preparation.domain.models import TableProfiles
from app.preparation.prompts.base import PromptDefinition, RenderedPrompt, render_prompt

FIELD_MAPPING_PROMPT = PromptDefinition(
    prompt_id="preparation.field_mapping",
    version="1.0.0",
    system_resource="field_mapping_system.md",
    user_resource="field_mapping_user.md",
)


def build_field_mapping_prompt(profiles: TableProfiles) -> RenderedPrompt:
    """把已提取的表结构摘要装配成字段识别提示词。

    Args:
        profiles: 程序提取的工作表表头、合并范围及有限样例，不是完整账单。

    Returns:
        系统消息、包含原样 JSON 摘要的用户消息及稳定的模板版本信息。
    """
    return render_prompt(
        FIELD_MAPPING_PROMPT, {"profiles_json": profiles.model_dump_json(indent=2)}
    )
