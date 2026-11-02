"""有限、可审计的公式引擎：只计算明确支持的表达式，不信任旧缓存。"""

import re
from fractions import Fraction
from xml.etree import ElementTree as ET

from app.delivery.models import FormulaProblem
from app.delivery.ooxml import Package, column, column_number
from app.matching.numbers import number, text
from app.preparation.infrastructure.xlsx import tag

REFERENCE = re.compile(
    r"(?<![\w.])(?:(?P<sheet>'(?:[^']|'')+'|[\w\u4e00-\u9fff]+)!)?(?P<col>\$?[A-Z]{1,3})(?P<row>\$?[1-9]\d*)(?![\w(])"
)
TOKEN = re.compile(
    r"\s*(?:((?:'(?:[^']|'')+'|[\w\u4e00-\u9fff]+)!\$?[A-Z]{1,3}\$?[1-9]\d*|\$?[A-Z]{1,3}\$?[1-9]\d*)|((?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?)|([A-Za-z_][A-Za-z_0-9]*)|([+*/^(),:%-]))"
)


class FormulaUnsupported(ValueError):
    """公式超出引擎的明确能力范围，验收必须记录 not_run。"""


def translate(
    formula: str,
    own_sheet: str,
    row_maps: dict[str, dict[int, int]],
    extras: dict[str, dict[int, int]] | None = None,
) -> str:
    """按插行后的真实位置重写工作簿内引用，不修改双引号内的业务文本。

    Args:
        formula: 不含前导等号或带等号的公式、定义名称或范围。
        own_sheet: 未限定引用所在的工作表。
        row_maps: 各工作表原始行到输出主行的映射。
        extras: 每个原始业务行后展开的附加行数，用于范围末端扩展。

    Returns:
        保留绝对引用和工作表限定的转换结果；不支持外部工作簿引用。
    """
    if "[" in formula or "]" in formula:
        raise FormulaUnsupported("外部工作簿或结构化表引用需要额外公式引擎")
    fragments = re.split(r'("(?:[^"]|"")*")', formula)
    for index in range(0, len(fragments), 2):
        fragment = fragments[index]

        def replace(match: re.Match[str], source_fragment: str = fragment) -> str:
            """替换一个真正单元格引用。

            Args:
                match: 已排除文本常量的引用匹配。
                source_fragment: 绑定当前不含文本常量的表达式片段。

            Returns:
                同一来源单元格在输出工作簿中的引用。
            """
            sheet_token = match.group("sheet")
            sheet_name = (
                own_sheet if sheet_token is None else sheet_token.strip("'").replace("''", "'")
            )
            # 范围右端可以省略工作表限定，继承紧邻范围左端的限定。
            if (
                sheet_token is None
                and match.start() > 0
                and source_fragment[match.start() - 1] == ":"
            ):
                previous = list(REFERENCE.finditer(source_fragment[: match.start() - 1]))
                if previous and previous[-1].group("sheet"):
                    sheet_name = previous[-1].group("sheet").strip("'").replace("''", "'")
            mapping = row_maps.get(sheet_name)
            if mapping is None:
                return match.group(0)
            original_row = int(match.group("row").lstrip("$"))
            shifted = mapping.get(
                original_row,
                original_row
                + sum(v for k, v in (extras or {}).get(sheet_name, {}).items() if k < original_row),
            )
            if (
                extras is not None
                and match.start() > 0
                and source_fragment[match.start() - 1] == ":"
            ):
                shifted += extras.get(sheet_name, {}).get(original_row, 0)
            row_token = ("$" if match.group("row").startswith("$") else "") + str(shifted)
            return (sheet_token + "!" if sheet_token else "") + match.group("col") + row_token

        fragments[index] = REFERENCE.sub(replace, fragment)
    return "".join(fragments)


def relative_formula(formula: str, row_delta: int, column_delta: int) -> str:
    """展开 Excel 共享公式的相对引用，再交给插行转换。

    Args:
        formula: 共享公式锚点的原始表达式。
        row_delta: 当前单元格与锚点的原始行差。
        column_delta: 当前单元格与锚点的原始列差。

    Returns:
        保留绝对引用的当前单元格完整公式。
    """
    fragments = re.split(r'("(?:[^"]|"")*")', formula)
    for index in range(0, len(fragments), 2):

        def replace(match: re.Match[str]) -> str:
            """转换一个共享引用。

            Args:
                match: 原始引用。

            Returns:
                相对平移后的引用。
            """
            c, r = match.group("col"), match.group("row")
            col_value = c if c.startswith("$") else column(column_number(c) + column_delta)
            row_value = r if r.startswith("$") else str(int(r) + row_delta)
            return (
                (match.group("sheet") + "!" if match.group("sheet") else "") + col_value + row_value
            )

        fragments[index] = REFERENCE.sub(replace, fragments[index])
    return "".join(fragments)


class Evaluator:
    """独立读取真实单元格计算 SUM、SUBTOTAL(9/109)、VALUE 和有理数算术。"""

    def __init__(self, roots: dict[str, ET.Element], strings: tuple[str, ...]) -> None:
        """建立真实工作簿计算视图。

        Args:
            roots: 所有工作表名称到实际 XML 根节点的映射。
            strings: 原共享字符串表。
        """
        self.cells: dict[tuple[str, str], ET.Element] = {
            (name, c.get("r", "")): c for name, root in roots.items() for c in root.iter(tag("c"))
        }
        self.strings = strings
        self.sheet_names = set(roots)
        self.visiting: set[tuple[str, str]] = set()
        self.cache: dict[tuple[str, str], Fraction] = {}
        self.hidden = {
            (name, int(row.get("r", "0")))
            for name, root in roots.items()
            for row in root.findall(f"{tag('sheetData')}/{tag('row')}")
            if row.get("hidden") == "1"
        }

    def value(self, sheet: str, coordinate: str, *, coerce_text: bool = False) -> Fraction:
        """计算真实数值或公式，SUM 对文本保持 Excel 的忽略语义。

        Args:
            sheet: 工作表名称。
            coordinate: 不带绝对标记的单元格坐标。
            coerce_text: VALUE 或直接算术是否允许解析数值文本。

        Returns:
            精确计算结果；引用错误、循环或未知函数抛出可定位错误。
        """
        key = (sheet, coordinate.replace("$", ""))
        if sheet not in self.sheet_names:
            raise ValueError("#REF! 工作表不存在")
        cell = self.cells.get(key)
        if cell is None:
            return Fraction()
        formula = cell.find(tag("f"))
        if formula is not None:
            if key in self.cache:
                return self.cache[key]
            if key in self.visiting:
                raise ValueError("#REF! 循环引用")
            if formula.get("t") in {"array", "dataTable", "shared"}:
                raise FormulaUnsupported("数组、数据表或未展开共享公式需要额外引擎")
            self.visiting.add(key)
            try:
                result = Expression(formula.text or "", sheet, self).evaluate()
                text(result)
                self.cache[key] = result
                return result
            finally:
                self.visiting.remove(key)
        cell_type = cell.get("t", "n")
        raw = cell.findtext(tag("v"))
        if cell_type == "e":
            raise ValueError(raw or "#VALUE!")
        if cell_type in {"inlineStr", "s", "str"}:
            if not coerce_text:
                return Fraction()
            raw = (
                self.strings[int(raw or "0")]
                if cell_type == "s"
                else "".join(n.text or "" for n in cell.iter(tag("t")))
                if cell_type == "inlineStr"
                else raw
            )
        if raw is None or raw == "":
            return Fraction()
        return number(raw)

    def range_values(
        self, sheet: str, start: str, end: str, *, subtotal: int | None = None
    ) -> list[Fraction]:
        """按 Excel 范围及 SUBTOTAL 语义枚举真实值。

        Args:
            sheet: 实际范围所属工作表。
            start: 左上角坐标。
            end: 右下角坐标。
            subtotal: None 表示 SUM；9/109 跳过嵌套小计，109 同时跳过隐藏行。

        Returns:
            不包含文本和不应累计的小计的数值列表。
        """
        first = re.fullmatch(r"\$?([A-Z]+)\$?(\d+)", start)
        last = re.fullmatch(r"\$?([A-Z]+)\$?(\d+)", end)
        if first is None or last is None:
            raise FormulaUnsupported("范围坐标不受支持")
        values: list[Fraction] = []
        start_row, end_row = int(first.group(2)), int(last.group(2))
        start_col, end_col = column_number(first.group(1)), column_number(last.group(1))
        if (
            start_row > end_row
            or start_col > end_col
            or (end_row - start_row + 1) * (end_col - start_col + 1) > 500000
        ):
            raise FormulaUnsupported("范围倒置或超过公式计算预算")
        for row in range(start_row, end_row + 1):
            for col in range(start_col, end_col + 1):
                coordinate = f"{column(col)}{row}"
                cell = self.cells.get((sheet, coordinate))
                formula = None if cell is None else cell.findtext(tag("f"))
                if (
                    subtotal is not None
                    and formula
                    and re.search(r"\bSUBTOTAL\s*\(", formula, re.IGNORECASE)
                ):
                    continue
                if subtotal == 109 and (sheet, row) in self.hidden:
                    continue
                values.append(self.value(sheet, coordinate))
        return values


class Expression:
    """不使用 eval 的受限公式解析器，遇到未知语法即停止并报告。"""

    def __init__(self, formula: str, sheet: str, evaluator: Evaluator) -> None:
        """切分公式为明确支持的符号。

        Args:
            formula: 单元格真实公式字符串。
            sheet: 当前工作表。
            evaluator: 读取实际值的工作簿计算上下文。
        """
        self.tokens: list[str] = []
        self.sheet, self.evaluator = sheet, evaluator
        self.position = 0
        source = formula.removeprefix("=").strip()
        offset = 0
        while offset < len(source):
            match = TOKEN.match(source, offset)
            if match is None:
                raise FormulaUnsupported(f"不支持的公式语法：{source[offset : offset + 32]}")
            self.tokens.append(next(value for value in match.groups() if value is not None))
            offset = match.end()

    def peek(self) -> str:
        """查看下一个词法单元。

        Returns:
            下一符号，空文本表示公式结束。
        """
        return self.tokens[self.position] if self.position < len(self.tokens) else ""

    def take(self, expected: str | None = None) -> str:
        """读取一个词法单元并检查所需符号。

        Args:
            expected: 可选的预期符号。

        Returns:
            实际读取的词法单元。
        """
        value = self.peek()
        if not value or expected is not None and value != expected:
            raise FormulaUnsupported("公式括号或分隔符不完整")
        self.position += 1
        return value

    def evaluate(self) -> Fraction:
        """计算完整表达式，拒绝被忽略的尾随内容。

        Returns:
            无静默舍入的精确公式结果。
        """
        result = self.addition()
        if self.peek():
            raise FormulaUnsupported("公式存在未处理的尾随语法")
        return result

    def addition(self) -> Fraction:
        """计算加减表达式。

        Returns:
            加减运算的精确结果。
        """
        value = self.multiplication()
        while self.peek() in {"+", "-"}:
            operation = self.take()
            right = self.multiplication()
            value = value + right if operation == "+" else value - right
        return value

    def multiplication(self) -> Fraction:
        """计算乘除表达式，除零必须形成失败。

        Returns:
            乘除运算的精确结果。
        """
        value = self.atom()
        while self.peek() in {"*", "/"}:
            operation = self.take()
            right = self.atom()
            if operation == "/" and right == 0:
                raise ValueError("#DIV/0!")
            value = value * right if operation == "*" else value / right
        return value

    def reference(self, token: str) -> tuple[str, str]:
        """解析带或不带表名的引用。

        Args:
            token: 当前引用词法单元。

        Returns:
            工作表名称和不带绝对标记的坐标。
        """
        if "!" in token:
            name, coordinate = token.rsplit("!", 1)
            return name.strip("'").replace("''", "'"), coordinate.replace("$", "")
        return self.sheet, token.replace("$", "")

    def atom(self) -> Fraction:
        """计算单元格引用、数值、括号或受支持函数。

        Returns:
            一个基本表达式的精确结果。
        """
        token = self.take()
        if token in {"+", "-"}:
            value = self.atom()
            return -value if token == "-" else value
        if token == "(":
            value = self.addition()
            self.take(")")
            return value
        if REFERENCE.fullmatch(token):
            sheet, coordinate = self.reference(token)
            return self.evaluator.value(sheet, coordinate, coerce_text=True)
        if self.peek() != "(":
            try:
                return number(token)
            except ValueError as exc:
                raise FormulaUnsupported(f"不支持的名称：{token}") from exc
        function = token.upper()
        if function not in {"SUM", "SUBTOTAL", "VALUE"}:
            raise FormulaUnsupported(f"未支持的函数：{function}")
        self.take("(")
        if function == "VALUE":
            value = self.addition()
            self.take(")")
            return value
        subtotal: int | None = None
        if function == "SUBTOTAL":
            code = self.take()
            if code not in {"9", "109"}:
                raise FormulaUnsupported("仅支持 SUBTOTAL(9/109)")
            subtotal = int(code)
            self.take(",")
        values: list[Fraction] = []
        while self.peek() != ")":
            if not self.peek():
                raise FormulaUnsupported("函数缺少右括号")
            token = self.peek()
            if (
                REFERENCE.fullmatch(token)
                and self.position + 1 < len(self.tokens)
                and self.tokens[self.position + 1] in {":", ",", ")"}
            ):
                self.take()
                sheet, coordinate = self.reference(token)
                if self.peek() == ":":
                    self.take()
                    other_sheet, end = self.reference(self.take())
                    if "!" in self.tokens[self.position - 1] and other_sheet != sheet:
                        raise FormulaUnsupported("范围两端工作表不一致")
                    values.extend(
                        self.evaluator.range_values(sheet, coordinate, end, subtotal=subtotal)
                    )
                else:
                    values.extend(
                        self.evaluator.range_values(
                            sheet, coordinate, coordinate, subtotal=subtotal
                        )
                    )
            else:
                values.append(self.addition())
            if self.peek() != ",":
                break
            self.take()
        self.take(")")
        return sum(values, Fraction())


def recalculate(package: Package) -> tuple[FormulaProblem, ...]:
    """重新计算真实公式并写入已验证缓存，失败时清除旧缓存避免冒充已重算。

    Args:
        package: 完成全部插行及范围变更后的工作簿。

    Returns:
        不支持或计算错误的明确问题列表。
    """
    roots = {name: package.sheet(name) for name in package.sheet_paths()}
    strings = tuple(
        "".join(n.text or "" for n in item.iter(tag("t"))) for item in parse_shared_strings(package)
    )
    evaluator = Evaluator(roots, strings)
    problems: list[FormulaProblem] = []
    for (name, coordinate), cell in evaluator.cells.items():
        if cell.find(tag("f")) is None:
            continue
        for node in list(cell.findall(tag("v"))):
            cell.remove(node)
        try:
            result = text(evaluator.value(name, coordinate))
            cell.set("t", "n")
            ET.SubElement(cell, tag("v")).text = result
            cell.set("s", str(package.style(int(cell.get("s", "0")))))
        except (ValueError, ZeroDivisionError) as exc:
            problems.append(
                FormulaProblem(
                    sheet_name=name,
                    coordinate=coordinate,
                    code="FORMULA_NOT_SUPPORTED"
                    if isinstance(exc, FormulaUnsupported)
                    else "FORMULA_CALCULATION_FAILED",
                    detail=str(exc),
                )
            )
    calculation = package.workbook.find(tag("calcPr"))
    if calculation is None:
        calculation = ET.SubElement(package.workbook, tag("calcPr"))
    calculation.set("calcMode", "auto")
    calculation.set("fullCalcOnLoad", "1" if problems else "0")
    calculation.set("forceFullCalc", "1" if problems else "0")
    return tuple(problems)


def parse_shared_strings(package: Package) -> list[ET.Element]:
    """读取共享字符串供独立公式语义使用。

    Args:
        package: 当前工作簿部件。

    Returns:
        原共享字符串项列表，缺失共享表时为空。
    """
    from app.preparation.infrastructure.xlsx import parse_xml

    return (
        list(parse_xml(package.parts["xl/sharedStrings.xml"]))
        if "xl/sharedStrings.xml" in package.parts
        else []
    )
