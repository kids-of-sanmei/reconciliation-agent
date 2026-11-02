"""将类型注解及函数说明要求作为持续验证约束。"""

import ast
from pathlib import Path


def test_new_production_functions_are_annotated_and_documented() -> None:
    """检查第一块生产代码的参数、返回类型及参数/返回说明完整性。"""
    backend = Path(__file__).resolve().parents[1]
    files = list((backend / "app/preparation").rglob("*.py"))
    files += list((backend / "app/core").rglob("*.py"))
    files += [
        backend / relative
        for relative in (
            "app/factory.py",
            "app/api/v1/preparation.py",
            "app/schema/preparation.py",
            "main.py",
        )
    ]
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            location = f"{path.name}:{node.lineno}:{node.name}"
            assert node.returns is not None, location
            documentation = ast.get_docstring(node)
            assert documentation, location
            parameters = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
            if node.args.vararg is not None:
                parameters.append(node.args.vararg)
            if node.args.kwarg is not None:
                parameters.append(node.args.kwarg)
            for parameter in parameters:
                if parameter.arg in {"self", "cls"}:
                    continue
                assert parameter.annotation is not None, f"{location}:{parameter.arg}"
                assert f"{parameter.arg}:" in documentation, (
                    f"{location}:{parameter.arg} 未说明含义"
                )
            if not (isinstance(node.returns, ast.Constant) and node.returns.value is None):
                assert "Returns:" in documentation, f"{location} 未说明返回值"
