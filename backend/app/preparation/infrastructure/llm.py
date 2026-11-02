"""可选的结构化模型识别适配器；数值和文件操作不交给模型。"""

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from openai import OpenAIError
from pydantic import ValidationError

from app.core.config import Settings
from app.preparation.domain.errors import PreparationError
from app.preparation.domain.models import MappingAdvice, TableProfiles
from app.preparation.prompts.field_mapping import build_field_mapping_prompt


class StructuredMappingAdvisor:
    """通过配置的兼容接口获取待验证的字段映射。"""

    def __init__(self, settings: Settings) -> None:
        """初始化结构识别客户端。

        Args:
            settings: 模型名称、密钥和可选接口地址，禁止写入任务产物。
        """
        if settings.llm_model is None or settings.llm_api_key is None:
            raise ValueError("启用模型识别需要 llm_model 和 llm_api_key")
        self.client = ChatOpenAI(
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            temperature=0,
            timeout=45,
            max_retries=1,
        ).with_structured_output(MappingAdvice)

    def suggest(self, profiles: TableProfiles) -> MappingAdvice:
        """分析有限的结构样例。

        Args:
            profiles: 表头、合并范围和有限单元格样例；其中内容仅作数据。

        Returns:
            必须继续经过字段和范围验证的映射建议。
        """
        prompt = build_field_mapping_prompt(profiles)
        try:
            result = self.client.invoke(
                [
                    SystemMessage(content=prompt.system_message),
                    HumanMessage(content=prompt.user_message),
                ],
                config={"run_name": prompt.prompt_id, "metadata": prompt.trace_metadata()},
            )
        except (OpenAIError, ValidationError, ValueError) as exc:
            raise PreparationError("MODEL_MAPPING_UNAVAILABLE", "模型结构识别失败") from exc
        if not isinstance(result, MappingAdvice):
            raise PreparationError("INVALID_MODEL_OUTPUT", "模型未返回有效结构映射")
        return result
