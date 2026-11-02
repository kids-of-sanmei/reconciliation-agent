"""HTTP JSON 与严格匹配领域对象的类型边界。"""

from pydantic import RootModel, model_validator

from app.matching.models import MatchConfirmation, MatchingRules
from app.schema.preparation import JSON_VALUE_ADAPTER


class MatchingRulesBody(RootModel[MatchingRules]):
    """JSON 数组和枚举只在 HTTP 边界转换，内部仍为严格类型。"""

    @model_validator(mode="before")
    @classmethod
    def parse_boundary(cls, value: object) -> MatchingRules:
        """转换合法 JSON 为严格规则。

        Args:
            value: FastAPI 解码后的 JSON 或内部规则对象。

        Returns:
            完成运行时校验的匹配规则。
        """
        if isinstance(value, MatchingRules):
            return value
        parsed = JSON_VALUE_ADAPTER.validate_python(value, strict=True)
        return MatchingRules.model_validate_json(JSON_VALUE_ADAPTER.dump_json(parsed))


class MatchConfirmationBody(RootModel[MatchConfirmation]):
    """确认请求包含完整审计及预期方案版本。"""

    @model_validator(mode="before")
    @classmethod
    def parse_boundary(cls, value: object) -> MatchConfirmation:
        """转换合法 JSON 为严格人工确认。

        Args:
            value: 解码后的 JSON 或内部确认对象。

        Returns:
            不容忍额外字段、隐式数字转换的确认对象。
        """
        if isinstance(value, MatchConfirmation):
            return value
        parsed = JSON_VALUE_ADAPTER.validate_python(value, strict=True)
        return MatchConfirmation.model_validate_json(JSON_VALUE_ADAPTER.dump_json(parsed))
