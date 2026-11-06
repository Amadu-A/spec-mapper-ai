# tests/test_architecture.py
"""Архитектурные ограничения импортов, документирования и отсутствия старого model runtime."""

from __future__ import annotations

import ast
from pathlib import Path


def test_dependency_direction():
    """Domain/application не зависят от HTTP, ORM, конкретных клиентов и infrastructure."""
    for directory in ("domain", "application"):
        for path in (Path("src") / directory).glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                assert not any(
                    module.startswith(
                        (
                            "fastapi",
                            "httpx",
                            "sqlalchemy",
                            "aio_pika",
                            "src.infrastructure",
                            "src.presentation",
                            "src.bootstrap",
                        )
                    )
                    for module in modules
                ), path


def test_source_documentation_and_path_headers():
    """Каждый production Python модуль, класс и функция документированы, путь указан первым."""
    for path in Path("src").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert source.startswith("# " + path.as_posix()), path
        tree = ast.parse(source)
        assert ast.get_docstring(tree), path
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                doc = ast.get_docstring(node)
                assert doc and any("а" <= character.lower() <= "я" for character in doc), (
                    path,
                    node.name,
                )


def test_no_local_model_runtime_or_inline_handlers():
    """Тяжёлые модели не входят в новый runtime; UI использует data hooks и безопасный DOM."""
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    assert "sentence-transformers" not in pyproject and "torch" not in pyproject
    for path in Path("src/web").rglob("*"):
        if path.suffix in {".html", ".js"}:
            text = path.read_text(encoding="utf-8")
            assert "onclick=" not in text and "innerHTML" not in text, path
