"""可向调用方解释的阶段性错误。"""


class PreparationError(Exception):
    """文件、字段或任务不符合处理契约时的业务异常。"""

    def __init__(self, code: str, message: str, *, http_status: int = 422) -> None:
        """构造错误。

        Args:
            code: 供程序判断的稳定错误码。
            message: 面向用户的具体失败原因。
            http_status: API 应返回的 HTTP 状态码。
        """
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
