"""第三块 HTTP JSON 到严格内部类型的边界适配。"""

from pydantic import RootModel, model_validator

from app.delivery.models import DeliveryRequest
from app.schema.preparation import JSON_VALUE_ADAPTER


class DeliveryRequestBody(RootModel[DeliveryRequest]):
    """只在 JSON 边界解析字面值，禁止隐式数值转换和额外字段。"""

    @model_validator(mode="before")
    @classmethod
    def parse_boundary(cls, value: object) -> DeliveryRequest:
        """将框架解码的 JSON 转为严格生成请求。

        Args:
            value: HTTP JSON 值或已验证的内部对象。

        Returns:
            含预期匹配版本和明确执行能力的请求。
        """
        if isinstance(value, DeliveryRequest):
            return value
        parsed = JSON_VALUE_ADAPTER.validate_python(value, strict=True)
        return DeliveryRequest.model_validate_json(JSON_VALUE_ADAPTER.dump_json(parsed))
