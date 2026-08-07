"""Every name `app/` uses is one it can reach.

A missing import is invisible until the line runs. ``session_connectors`` went
out with the enqueue calling it and no import above it: the module imported
fine, 594 tests passed, and the first real turn raised ``NameError`` in
production. Nothing here lints — no ruff, no flake8 — so this is the check.

Deliberately narrow. It resolves names against the module's own bindings, its
imports, and builtins, and reports what is left. It is not a type checker and
not a style pass; it answers one question, which is the one that broke.
"""

from __future__ import annotations

import ast
import builtins
import pathlib

APP = pathlib.Path(__file__).resolve().parent.parent / "app"


class _Bindings(ast.NodeVisitor):
    """Names a module binds anywhere in it, at any depth.

    Scope-insensitive on purpose: a name bound in one function and read in
    another is a bug this will miss, and the alternative is a scope resolver —
    far more code, and more of it to be wrong. Undershooting keeps the check
    trustworthy, which is what makes a red run worth acting on.
    """

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.names.add(alias.asname or alias.name.split(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self.names.add(alias.asname or alias.name)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)

    def visit_arg(self, node: ast.arg) -> None:
        self.names.add(node.arg)

    def _definition(self, node) -> None:
        self.names.add(node.name)
        self.generic_visit(node)

    visit_FunctionDef = _definition
    visit_AsyncFunctionDef = _definition
    visit_ClassDef = _definition

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.names.add(node.name)
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        self.names.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.names.update(node.names)


def _unresolved(source: str) -> set[str]:
    tree = ast.parse(source)
    bindings = _Bindings()
    bindings.visit(tree)
    # Module-level names the interpreter supplies; ``__path__`` exists only in
    # a package's ``__init__``, which is where the auto-import walk reads it.
    known = bindings.names | set(dir(builtins)) | {
        "__file__", "__name__", "__doc__", "__path__",
    }
    read = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    return read - known


def test_no_name_is_used_without_being_reachable():
    offenders = {
        str(module.relative_to(APP.parent)): sorted(names)
        for module in sorted(APP.rglob("*.py"))
        if (names := _unresolved(module.read_text()))
    }

    assert offenders == {}
