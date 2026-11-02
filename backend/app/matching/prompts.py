"""逐行疑难匹配的独立提示词定义，复用已有版本化资源加载器。"""

from app.matching.models import RowContext
from app.preparation.prompts.base import PromptDefinition, RenderedPrompt, render_prompt

MATCHING_PROMPT = PromptDefinition(
    prompt_id="matching.row_review",
    version="1.0.0",
    system_resource="matching_system.md",
    user_resource="matching_user.md",
)


def build_matching_prompt(context: RowContext) -> RenderedPrompt:
    """将当前行和程序证据装配为有界语义复核输入。

    Args:
        context: 当前 A、必要相邻行、真实 B 候选及程序检查结果。

    Returns:
        带版本和模板摘要的系统、用户消息。
    """
    return render_prompt(MATCHING_PROMPT, {"context_json": context.model_dump_json(indent=2)})
