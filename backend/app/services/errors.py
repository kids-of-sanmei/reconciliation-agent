"""上传业务异常，由 API 层转换为对应的 HTTP 响应。"""


class UploadError(ValueError):
    """上传文件不满足业务校验规则。"""


class UnsupportedExcelTypeError(UploadError):
    """上传文件的扩展名不在允许的 Excel 格式中。"""


class EmptyUploadError(UploadError):
    """文件内容为空，不能作为有效上传保存。"""


class UploadSizeLimitError(UploadError):
    """实际读取的文件大小超过允许的上限。"""


class InvalidExcelError(UploadError):
    """文件内容不是能够正常读取的 Excel 工作簿。"""


class InvalidHeaderRangeError(UploadError):
    """指定的表头行范围无效或无法完整展开。"""
