"""HTTP JSON 边界适配，保持内部领域模型的严格 Python 类型。"""

from pydantic import JsonValue, RootModel, TypeAdapter, model_validator

from app.preparation.domain.models import Confirmation

JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class ConfirmationBody(RootModel[Confirmation]):
    """将 HTTP 数组和枚举文本按 JSON 契约解析为严格内部类型。"""

    @model_validator(mode="before")
    @classmethod
    def parse_json_boundary(cls, value: object) -> Confirmation:
        """按真实 JSON 语义解析，避免把合法 JSON 数组误判为内部 tuple 错误。

        Args:
            value: 框架解码后的 JSON 对象或已经验证的确认对象。

        Returns:
            严格校验通过的 Confirmation，不容忍错误字段或数值类型。
        """
        if isinstance(value, Confirmation):
            return value
        json_value = JSON_VALUE_ADAPTER.validate_python(value, strict=True)
        return Confirmation.model_validate_json(JSON_VALUE_ADAPTER.dump_json(json_value))
