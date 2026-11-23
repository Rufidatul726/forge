from __future__ import annotations

import ast
import logging
from typing import Any

from forge.intelligence.models import (
    ExtractedFileSymbols,
    ImportRecord,
    ReferenceKind,
    ReferenceRecord,
    SymbolKind,
    SymbolRecord,
)
from forge.intelligence.parsers.base import BaseParser

logger = logging.getLogger("forge.intelligence.parsers.python")


class PythonParser(BaseParser):
    """AST-based symbol and approximate reference parser for Python source files.

    Distinction Note:
    Resolves local and imported names using AST identifier and import mapping for navigation.
    This provides approximate references, not a full compiler-grade call graph.
    """

    def parse(self, rel_path: str, content: str) -> ExtractedFileSymbols:
        symbols: list[SymbolRecord] = []
        imports: list[ImportRecord] = []
        references: list[ReferenceRecord] = []
        errors: list[str] = []

        try:
            tree = ast.parse(content, filename=rel_path)
        except SyntaxError as exc:
            errors.append(f"SyntaxError on line {exc.lineno}: {exc.msg}")
            return ExtractedFileSymbols(path=rel_path, symbols=symbols, imports=imports, references=references, errors=errors)
        except Exception as exc:
            errors.append(f"Parsing error: {exc}")
            return ExtractedFileSymbols(path=rel_path, symbols=symbols, imports=imports, references=references, errors=errors)

        # 1. First pass: extract imports and local definitions for reference resolution
        imported_names_map: dict[str, str] = {}  # local_name -> original_module/symbol
        local_symbols_set: set[str] = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name_used = alias.asname or alias.name
                    imported_names_map[name_used] = alias.name
                    imports.append(
                        ImportRecord(
                            source_path=rel_path,
                            module=alias.name,
                            imported_names=[alias.name],
                            alias=alias.asname,
                            line_number=node.lineno,
                            is_relative=False,
                        )
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                is_rel = node.level > 0
                names = []
                for alias in node.names:
                    name_used = alias.asname or alias.name
                    imported_names_map[name_used] = f"{module}.{alias.name}" if module else alias.name
                    names.append(alias.name)
                imports.append(
                    ImportRecord(
                        source_path=rel_path,
                        module=module,
                        imported_names=names,
                        line_number=node.lineno,
                        is_relative=is_rel,
                    )
                )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                local_symbols_set.add(node.name)

        # 2. Second pass: hierarchical traversal for symbols and parent-child tracking
        class SymbolVisitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.scope_stack: list[str] = []

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                parent = self.scope_stack[-1] if self.scope_stack else None
                doc = ast.get_docstring(node)
                end_line = getattr(node, "end_lineno", node.lineno)

                # Base class references
                for base in node.bases:
                    base_name = None
                    if isinstance(base, ast.Name):
                        base_name = base.id
                    elif isinstance(base, ast.Attribute):
                        base_name = base.attr

                    if base_name:
                        is_res = base_name in imported_names_map or base_name in local_symbols_set
                        target = imported_names_map.get(base_name) or (rel_path if base_name in local_symbols_set else None)
                        references.append(
                            ReferenceRecord(
                                source_path=rel_path,
                                symbol_name=base_name,
                                line_number=node.lineno,
                                kind=ReferenceKind.INHERITANCE,
                                target_path=target,
                                target_symbol_name=base_name,
                                is_resolved=is_res,
                            )
                        )

                symbols.append(
                    SymbolRecord(
                        name=node.name,
                        kind=SymbolKind.CLASS,
                        path=rel_path,
                        line_number=node.lineno,
                        end_line_number=end_line,
                        parent_name=parent,
                        docstring=doc,
                    )
                )

                self.scope_stack.append(node.name)
                self.generic_visit(node)
                self.scope_stack.pop()

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self._handle_function(node)

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self._handle_function(node)

            def _handle_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                parent = self.scope_stack[-1] if self.scope_stack else None
                kind = SymbolKind.METHOD if parent is not None else SymbolKind.FUNCTION
                doc = ast.get_docstring(node)
                end_line = getattr(node, "end_lineno", node.lineno)

                # Build signature preview
                params = [arg.arg for arg in node.args.args]
                sig = f"def {node.name}({', '.join(params)})"

                symbols.append(
                    SymbolRecord(
                        name=node.name,
                        kind=kind,
                        path=rel_path,
                        line_number=node.lineno,
                        end_line_number=end_line,
                        parent_name=parent,
                        signature=sig,
                        docstring=doc,
                    )
                )

                self.scope_stack.append(node.name)
                self.generic_visit(node)
                self.scope_stack.pop()

            def visit_Assign(self, node: ast.Assign) -> None:
                # Top-level constant declarations
                if not self.scope_stack:
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id.isupper():
                            symbols.append(
                                SymbolRecord(
                                    name=target.id,
                                    kind=SymbolKind.CONSTANT,
                                    path=rel_path,
                                    line_number=node.lineno,
                                    end_line_number=getattr(node, "end_lineno", node.lineno),
                                )
                            )
                self.generic_visit(node)

            def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
                if not self.scope_stack and isinstance(node.target, ast.Name):
                    if node.target.id.isupper():
                        symbols.append(
                            SymbolRecord(
                                name=node.target.id,
                                kind=SymbolKind.CONSTANT,
                                path=rel_path,
                                line_number=node.lineno,
                                end_line_number=getattr(node, "end_lineno", node.lineno),
                            )
                        )
                self.generic_visit(node)

            def visit_Call(self, node: ast.Call) -> None:
                called_name = None
                if isinstance(node.func, ast.Name):
                    called_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    called_name = node.func.attr

                if called_name and not called_name.startswith("__"):
                    is_res = called_name in imported_names_map or called_name in local_symbols_set
                    target = imported_names_map.get(called_name) or (rel_path if called_name in local_symbols_set else None)
                    references.append(
                        ReferenceRecord(
                            source_path=rel_path,
                            symbol_name=called_name,
                            line_number=node.lineno,
                            kind=ReferenceKind.CALL,
                            target_path=target,
                            target_symbol_name=called_name,
                            is_resolved=is_res,
                        )
                    )
                self.generic_visit(node)

        visitor = SymbolVisitor()
        visitor.visit(tree)

        return ExtractedFileSymbols(
            path=rel_path,
            symbols=symbols,
            imports=imports,
            references=references,
            errors=errors,
        )
