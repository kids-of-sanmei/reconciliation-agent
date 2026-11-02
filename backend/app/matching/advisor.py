"""匹配模型端口及供应商适配器，沿用第一块的模型连接配置。"""

import json
from typing import Protocol

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from openai import OpenAIError
from pydantic import ValidationError

from app.core.config import Settings
from app.matching.models import ModelProposal, RowContext
from app.matching.prompts import build_matching_prompt
from app.preparation.domain.errors import PreparationError
from app.preparation.infrastructure.llm import OllamaResponse


class MatchingAdvisor(Protocol):
    """模型只返回建议，正式匹配由程序重新验证。"""

    def suggest(self, context: RowContext) -> ModelProposal:
        """分析当前疑难行的程序证据。

        Args:
            context: 有界来源、候选及检查上下文。

        Returns:
            尚未成为正式关系的结构化语义建议。
        """
        ...


class StructuredMatchingAdvisor:
    """通过兼容 OpenAI 的结构化接口调用复核模型。"""

    def __init__(self, settings: Settings) -> None:
        """创建模型客户端。

        Args:
            settings: 名称、密钥、地址和单次调用超时配置。
        """
        if not settings.llm_model or settings.llm_api_key is None:
            raise ValueError("匹配模型需要名称和密钥")
        self.client = ChatOpenAI(
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            temperature=0,
            timeout=settings.llm_timeout_seconds,
            max_retries=0,
        ).with_structured_output(ModelProposal)

    def suggest(self, context: RowContext) -> ModelProposal:
        """调用模型并验证响应结构。

        Args:
            context: 当前疑难行的有限证据。

        Returns:
            需程序继续校验的模型建议。
        """
        prompt = build_matching_prompt(context)
        try:
            result = self.client.invoke(
                [
                    SystemMessage(content=prompt.system_message),
                    HumanMessage(content=prompt.user_message),
                ],
                config={"run_name": prompt.prompt_id, "metadata": prompt.trace_metadata()},
            )
            if not isinstance(result, ModelProposal):
                raise TypeError("模型响应结构无效")
            return result
        except (OpenAIError, ValidationError, ValueError, TypeError) as exc:
            raise PreparationError(
                "MATCH_MODEL_UNAVAILABLE", "匹配模型响应失败，已保留候选供人工复核"
            ) from exc


class OllamaMatchingAdvisor:
    """使用 Ollama 原生结构化输出，不需要模型密钥。"""

    def __init__(self, settings: Settings) -> None:
        """保存本地模型连接设置。

        Args:
            settings: 本地模型名称、服务根地址及资源限制。
        """
        self.settings = settings

    def suggest(self, context: RowContext) -> ModelProposal:
        """向本地模型发送有界证据并校验完整 JSON 输出。

        Args:
            context: 当前行、候选和规则，不包含完整工作簿。

        Returns:
            未经业务确认的结构化建议。
        """
        prompt = build_matching_prompt(context)
        schema = ModelProposal.model_json_schema()
        try:
            response = httpx.post(
                f"{(self.settings.llm_base_url or 'http://127.0.0.1:11434').rstrip('/')}/api/chat",
                json={
                    "model": self.settings.llm_model,
                    "messages": [
                        {"role": "system", "content": prompt.system_message},
                        {
                            "role": "user",
                            "content": prompt.user_message
                            + "\nJSON Schema:\n"
                            + json.dumps(schema, ensure_ascii=False),
                        },
                    ],
                    "format": schema,
                    "stream": False,
                    "think": False,
                    "options": {"temperature": 0, "num_ctx": self.settings.llm_context_tokens},
                },
                timeout=self.settings.llm_timeout_seconds,
            )
            response.raise_for_status()
            result = OllamaResponse.model_validate_json(response.content)
            if not result.done or result.done_reason == "length":
                raise ValueError("模型输出未完整结束")
            return ModelProposal.model_validate_json(result.message.content)
        except (httpx.HTTPError, ValidationError, ValueError) as exc:
            raise PreparationError(
                "MATCH_MODEL_UNAVAILABLE", "本地匹配模型响应失败，已保留候选供人工复核"
            ) from exc
