from __future__ import annotations

import re
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

# Regex patterns for JavaScript and TypeScript constructs
RE_IMPORT_ES6 = re.compile(
    r"""import\s+(?:(?:(?P<default>[a-zA-Z_$][a-zA-Z0-9_$]*)\s*,\s*)?\{\s*(?P<named>[^}]+)\s*\}|(?P<all>\*\s+as\s+[a-zA-Z_$][a-zA-Z0-9_$]*)|(?P<single_default>[a-zA-Z_$][a-zA-Z0-9_$]*))\s+from\s+['"](?P<module>[^'"]+)['"]""",
    re.MULTILINE,
)
RE_IMPORT_REQUIRE = re.compile(
    r"""(?:const|let|var)\s+(?:(?P<default>[a-zA-Z_$][a-zA-Z0-9_$]*)|(?:\{\s*(?P<named>[^}]+)\s*\}))\s*=\s*require\(['"](?P<module>[^'"]+)['"]\)"""
)

RE_CLASS = re.compile(
    r"""(?:export\s+)?(?:default\s+)?class\s+(?P<name>[a-zA-Z_$][a-zA-Z0-9_$]*)(?:\s+extends\s+(?P<extends>[a-zA-Z0-9_$.]+))?(?:\s+implements\s+(?P<implements>[^{]+))?\s*\{"""
)

RE_INTERFACE = re.compile(
    r"""(?:export\s+)?interface\s+(?P<name>[a-zA-Z_$][a-zA-Z0-9_$]*)(?:\s+extends\s+(?P<extends>[^{]+))?\s*\{"""
)

RE_TYPE_ALIAS = re.compile(
    r"""(?:export\s+)?type\s+(?P<name>[a-zA-Z_$][a-zA-Z0-9_$]*)(?:<[^>]+>)?\s*="""
)

RE_FUNCTION_DECL = re.compile(
    r"""(?:export\s+)?(?:default\s+)?(?:async\s+)?function(?:\s*\*)?\s+(?P<name>[a-zA-Z_$][a-zA-Z0-9_$]*)\s*\((?P<params>[^)]*)\)"""
)

RE_CONST_FUNCTION = re.compile(
    r"""(?:export\s+)?const\s+(?P<name>[a-zA-Z_$][a-zA-Z0-9_$]*)\s*=\s*(?:async\s*)?(?:\((?P<params>[^)]*)\)|[a-zA-Z_$][a-zA-Z0-9_$]*)\s*=>"""
)

RE_CONST_VARIABLE = re.compile(
    r"""(?:export\s+)?const\s+(?P<name>[A-Z0-9_]{2,})\s*[:=]"""
)

RE_METHOD = re.compile(
    r"""^\s*(?:(?:public|private|protected|static|async|readonly)\s+)*(?P<name>[a-zA-Z_$][a-zA-Z0-9_$]*)\s*\((?P<params>[^)]*)\)(?:\s*:\s*[^{]+)?\s*\{"""
)

RE_CALL = re.compile(r"""\b(?P<name>[a-zA-Z_$][a-zA-Z0-9_$]*)\s*\(""")
RE_NEW = re.compile(r"""\bnew\s+(?P<name>[a-zA-Z_$][a-zA-Z0-9_$]*)\s*\(""")


class JavaScriptTypeScriptParser(BaseParser):
    """Symbol and reference parser for JavaScript, TypeScript, TSX, and JSX files.

    Distinction Note:
    Uses regular expressions and line-based scanning to detect definitions, imports,
    and identifier usages. This is designed for search and navigation, not a complete
    cross-file call graph, and does not evaluate complex TypeScript expressions or types.
    """

    def parse(self, rel_path: str, content: str) -> ExtractedFileSymbols:
        symbols: list[SymbolRecord] = []
        imports: list[ImportRecord] = []
        references: list[ReferenceRecord] = []
        errors: list[str] = []

        lines = content.splitlines()
        imported_names_map: dict[str, str] = {}
        local_symbols_set: set[str] = set()

        # Pass 1: Extract imports
        for line_idx, line in enumerate(lines, start=1):
            line_str = line.strip()
            if not line_str or line_str.startswith("//") or line_str.startswith("/*"):
                continue

            # ES6 imports
            m_es6 = RE_IMPORT_ES6.search(line)
            if m_es6:
                mod = m_es6.group("module")
                names: list[str] = []
                default_name = m_es6.group("default") or m_es6.group("single_default")
                if default_name:
                    d_name = default_name.strip()
                    names.append(d_name)
                    imported_names_map[d_name] = f"{mod}.default"
                if m_es6.group("all"):
                    all_name = m_es6.group("all").split()[-1]
                    names.append(all_name)
                    imported_names_map[all_name] = mod
                if m_es6.group("named"):
                    for item in m_es6.group("named").split(","):
                        item = item.strip()
                        if " as " in item:
                            orig, alias = item.split(" as ")
                            names.append(orig.strip())
                            imported_names_map[alias.strip()] = f"{mod}.{orig.strip()}"
                        elif item:
                            names.append(item)
                            imported_names_map[item] = f"{mod}.{item}"

                imports.append(
                    ImportRecord(
                        source_path=rel_path,
                        module=mod,
                        imported_names=names,
                        line_number=line_idx,
                        is_relative=mod.startswith("."),
                    )
                )
                continue

            # Require calls
            m_req = RE_IMPORT_REQUIRE.search(line)
            if m_req:
                mod = m_req.group("module")
                names = []
                if m_req.group("default"):
                    d_name = m_req.group("default").strip()
                    names.append(d_name)
                    imported_names_map[d_name] = mod
                if m_req.group("named"):
                    for item in m_req.group("named").split(","):
                        item = item.strip()
                        if item:
                            names.append(item)
                            imported_names_map[item] = f"{mod}.{item}"

                imports.append(
                    ImportRecord(
                        source_path=rel_path,
                        module=mod,
                        imported_names=names,
                        line_number=line_idx,
                        is_relative=mod.startswith("."),
                    )
                )

        # Pass 2: Line-by-line stateful parse tracking classes, interfaces, functions, methods
        class_stack: list[tuple[str, int]] = []  # (class_name, brace_depth)
        brace_depth = 0

        for line_idx, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
                # Still track braces even in partial lines if not pure comment
                pass

            # Check class declaration
            m_class = RE_CLASS.search(line)
            if m_class:
                cls_name = m_class.group("name")
                parent = class_stack[-1][0] if class_stack else None
                symbols.append(
                    SymbolRecord(
                        name=cls_name,
                        kind=SymbolKind.CLASS,
                        path=rel_path,
                        line_number=line_idx,
                        end_line_number=line_idx,
                        parent_name=parent,
                    )
                )
                local_symbols_set.add(cls_name)

                # Extends reference
                extends_name = m_class.group("extends")
                if extends_name:
                    is_res = extends_name in imported_names_map or extends_name in local_symbols_set
                    references.append(
                        ReferenceRecord(
                            source_path=rel_path,
                            symbol_name=extends_name,
                            line_number=line_idx,
                            kind=ReferenceKind.INHERITANCE,
                            target_path=imported_names_map.get(extends_name),
                            target_symbol_name=extends_name,
                            is_resolved=is_res,
                        )
                    )

                class_stack.append((cls_name, brace_depth + 1))

            # Check interface declaration
            m_iface = RE_INTERFACE.search(line)
            if m_iface:
                iface_name = m_iface.group("name")
                symbols.append(
                    SymbolRecord(
                        name=iface_name,
                        kind=SymbolKind.INTERFACE,
                        path=rel_path,
                        line_number=line_idx,
                        end_line_number=line_idx,
                    )
                )
                local_symbols_set.add(iface_name)

            # Check type alias
            m_type = RE_TYPE_ALIAS.search(line)
            if m_type:
                t_name = m_type.group("name")
                symbols.append(
                    SymbolRecord(
                        name=t_name,
                        kind=SymbolKind.TYPE_ALIAS,
                        path=rel_path,
                        line_number=line_idx,
                        end_line_number=line_idx,
                    )
                )
                local_symbols_set.add(t_name)

            # Check function declaration
            m_func = RE_FUNCTION_DECL.search(line)
            if m_func and not m_class:
                f_name = m_func.group("name")
                params = m_func.group("params") or ""
                symbols.append(
                    SymbolRecord(
                        name=f_name,
                        kind=SymbolKind.FUNCTION,
                        path=rel_path,
                        line_number=line_idx,
                        end_line_number=line_idx,
                        signature=f"function {f_name}({params.strip()})",
                    )
                )
                local_symbols_set.add(f_name)

            # Check arrow function / const function
            m_cfunc = RE_CONST_FUNCTION.search(line)
            if m_cfunc:
                cf_name = m_cfunc.group("name")
                symbols.append(
                    SymbolRecord(
                        name=cf_name,
                        kind=SymbolKind.FUNCTION,
                        path=rel_path,
                        line_number=line_idx,
                        end_line_number=line_idx,
                        signature=f"const {cf_name} = (...) =>",
                    )
                )
                local_symbols_set.add(cf_name)

            # Check constant variable
            m_cvar = RE_CONST_VARIABLE.search(line)
            if m_cvar and not m_cfunc:
                cv_name = m_cvar.group("name")
                symbols.append(
                    SymbolRecord(
                        name=cv_name,
                        kind=SymbolKind.CONSTANT,
                        path=rel_path,
                        line_number=line_idx,
                        end_line_number=line_idx,
                    )
                )

            # Check method inside class
            if class_stack:
                cur_class = class_stack[-1][0]
                m_method = RE_METHOD.search(line)
                if m_method and not m_class and not m_func:
                    m_name = m_method.group("name")
                    if m_name not in {"if", "for", "while", "switch", "catch"}:
                        symbols.append(
                            SymbolRecord(
                                name=m_name,
                                kind=SymbolKind.METHOD,
                                path=rel_path,
                                line_number=line_idx,
                                end_line_number=line_idx,
                                parent_name=cur_class,
                                signature=f"{m_name}()",
                            )
                        )

            # Track references: new Foo(...)
            for m_new in RE_NEW.finditer(line):
                n_name = m_new.group("name")
                is_res = n_name in imported_names_map or n_name in local_symbols_set
                references.append(
                    ReferenceRecord(
                        source_path=rel_path,
                        symbol_name=n_name,
                        line_number=line_idx,
                        kind=ReferenceKind.INSTANTIATION,
                        target_path=imported_names_map.get(n_name),
                        target_symbol_name=n_name,
                        is_resolved=is_res,
                    )
                )

            # Track references: calls foo(...)
            for m_call in RE_CALL.finditer(line):
                c_name = m_call.group("name")
                if c_name not in {"if", "for", "while", "switch", "catch", "function", "return", "import", "require"}:
                    is_res = c_name in imported_names_map or c_name in local_symbols_set
                    references.append(
                        ReferenceRecord(
                            source_path=rel_path,
                            symbol_name=c_name,
                            line_number=line_idx,
                            kind=ReferenceKind.CALL,
                            target_path=imported_names_map.get(c_name),
                            target_symbol_name=c_name,
                            is_resolved=is_res,
                        )
                    )

            # Update brace depth
            brace_delta = line.count("{") - line.count("}")
            brace_depth += brace_delta
            if brace_depth < 0:
                brace_depth = 0

            # Pop class if we exited its brace scope
            while class_stack and brace_depth < class_stack[-1][1]:
                class_stack.pop()

        return ExtractedFileSymbols(
            path=rel_path,
            symbols=symbols,
            imports=imports,
            references=references,
            errors=errors,
        )
