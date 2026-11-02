"""结构化模型识别适配器；数值和文件操作不交给模型。"""

import json

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from openai import OpenAIError
from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.config import Settings
from app.preparation.domain.errors import PreparationError
from app.preparation.domain.models import FieldName, MappingAdvice, TableProfiles
from app.preparation.prompts.field_mapping import (
    build_field_mapping_prompt,
    build_field_mapping_repair_prompt,
)


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
            timeout=settings.llm_timeout_seconds,
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


class OllamaMessage(BaseModel):
    """只解析本地模型的最终内容，不保存思考内容。"""

    model_config = ConfigDict(strict=True)
    content: str


class OllamaResponse(BaseModel):
    """校验 Ollama 非流式响应，忽略与业务无关的运行指标。"""

    model_config = ConfigDict(strict=True)
    message: OllamaMessage
    done: bool
    done_reason: str | None = None


class OllamaMappingAdvisor:
    """使用本机 Ollama 原生 JSON Schema 接口识别表结构，无需密钥。"""

    def __init__(self, settings: Settings) -> None:
        """配置本地模型的调用边界。

        Args:
            settings: 模型名称、本机服务地址、超时和上下文长度。
        """
        if not settings.llm_model:
            raise ValueError("启用 Ollama 识别需要 llm_model")
        self.model = settings.llm_model
        self.url = (settings.llm_base_url or "http://127.0.0.1:11434").rstrip("/")
        self.timeout = settings.llm_timeout_seconds
        self.context_tokens = settings.llm_context_tokens

    def suggest(self, profiles: TableProfiles) -> MappingAdvice:
        """在本地分析有限结构证据并校验结构化响应。

        Args:
            profiles: 表头、明细样例及尾行证据，所有单元格仅作为数据。

        Returns:
            仍需经过工作表、字段列和业务范围校验的映射建议。
        """
        prompt = build_field_mapping_prompt(profiles)
        schema = MappingAdvice.model_json_schema()
        # Ollama 对 propertyNames 枚举的约束不足，显式列出允许的业务字段键。
        schema["$defs"]["SheetMapping"]["properties"]["fields"] = {
            "type": "object",
            "properties": {
                field.value: {"type": "string", "pattern": "^[A-Z]{1,3}$"} for field in FieldName
            },
            "additionalProperties": False,
        }
        messages = [
            {"role": "system", "content": prompt.system_message},
            {
                "role": "user",
                "content": prompt.user_message
                + "\n\nJSON Schema:\n"
                + json.dumps(schema, ensure_ascii=False),
            },
        ]
        try:
            for attempt in range(2):
                response = httpx.post(
                    f"{self.url}/api/chat",
                    json={
                        "model": self.model,
                        "messages": messages,
                        "format": schema,
                        "stream": False,
                        "think": False,
                        "options": {"temperature": 0, "num_ctx": self.context_tokens},
                    },
                    timeout=self.timeout,
                )
                response.raise_for_status()
                result = OllamaResponse.model_validate_json(response.content)
                if not result.done or result.done_reason == "length":
                    raise ValueError("本地模型输出未完整结束")
                try:
                    return MappingAdvice.model_validate_json(result.message.content)
                except ValidationError as exc:
                    if attempt == 1:
                        raise
                    messages.extend(
                        [
                            {"role": "assistant", "content": result.message.content},
                            {
                                "role": "user",
                                "content": build_field_mapping_repair_prompt(
                                    json.dumps(
                                        exc.errors(
                                            include_url=False,
                                            include_context=False,
                                            include_input=False,
                                        ),
                                        ensure_ascii=False,
                                    )
                                ).user_message,
                            },
                        ]
                    )
            raise ValueError("模型未返回有效映射")
        except (httpx.HTTPError, ValidationError, ValueError) as exc:
            raise PreparationError(
                "MODEL_MAPPING_UNAVAILABLE", "本地 Ollama 字段识别失败，请检查模型和服务状态"
            ) from exc
