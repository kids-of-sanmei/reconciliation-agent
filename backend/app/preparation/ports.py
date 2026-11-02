"""可替换的基础设施契约，业务编排只依赖这些类型。"""

from pathlib import Path
from typing import Protocol

from app.preparation.domain.models import (
    AuditTrail,
    BusinessRecord,
    MappingAdvice,
    SheetMapping,
    SourceFile,
    TableProfiles,
    WorkbookSnapshot,
)


class WorkbookGateway(Protocol):
    """工作簿读取与底稿写入接口，便于扩展其他格式。"""

    def read(self, path: Path, source: SourceFile) -> WorkbookSnapshot:
        """解析工作簿。

        Args:
            path: 已保存的来源文件绝对路径。
            source: 来源身份、摘要和角色。

        Returns:
            保留来源精度与结构的快照。
        """
        ...

    def create_baseline(
        self,
        path: Path,
        snapshot: WorkbookSnapshot,
        mappings: tuple[SheetMapping, ...],
        records: tuple[BusinessRecord, ...],
        period: str,
        formula_values: dict[str, str],
    ) -> tuple[bytes, AuditTrail]:
        """创建独立 C 底稿。

        Args:
            path: 原始 A 工作簿路径。
            snapshot: A 原始结构快照。
            mappings: 已验证的 A 业务范围。
            records: 规范化 A 明细，包含填充日期。
            period: 已确认的对账年月。
            formula_values: 人工补充且留有审计的公式结果。

        Returns:
            完整底稿文件字节及清理操作审计记录。
        """
        ...


class MappingAdvisor(Protocol):
    """陌生表结构的语义识别接口。"""

    def suggest(self, profiles: TableProfiles) -> MappingAdvice:
        """根据有限结构摘要提出映射。

        Args:
            profiles: 表头与有限样例，不包含工作簿全部明细。

        Returns:
            仍需程序验证的字段和范围建议。
        """
        ...
