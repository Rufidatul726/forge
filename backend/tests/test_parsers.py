from forge.intelligence.models import ReferenceKind, SymbolKind
from forge.intelligence.parsers.javascript import JavaScriptTypeScriptParser
from forge.intelligence.parsers.python import PythonParser
from forge.intelligence.parsers.registry import get_parser_for_language


def test_python_parser_classes_functions_and_methods():
    code = '''import os
from math import sqrt, pi as PI_CONST
from typing import Optional

GLOBAL_LIMIT = 100

def standalone_func(a, b):
    """A standalone function."""
    return a + b

class Calculator:
    """A sample calculator."""
    def __init__(self, start: int = 0):
        self.value = start

    def add(self, x):
        res = standalone_func(self.value, x)
        return sqrt(res)

    async def compute_async(self):
        return self.value
'''
    parser = PythonParser()
    extracted = parser.parse("calc.py", code)

    assert not extracted.errors
    assert len(extracted.imports) == 3

    # Check imported names
    math_imp = next(i for i in extracted.imports if i.module == "math")
    assert "sqrt" in math_imp.imported_names

    # Check symbols
    symbols_by_name = {s.name: s for s in extracted.symbols}
    assert "GLOBAL_LIMIT" in symbols_by_name
    assert symbols_by_name["GLOBAL_LIMIT"].kind == SymbolKind.CONSTANT

    assert "standalone_func" in symbols_by_name
    assert symbols_by_name["standalone_func"].kind == SymbolKind.FUNCTION
    assert symbols_by_name["standalone_func"].parent_name is None
    assert "A standalone function." in (symbols_by_name["standalone_func"].docstring or "")

    assert "Calculator" in symbols_by_name
    assert symbols_by_name["Calculator"].kind == SymbolKind.CLASS

    assert "add" in symbols_by_name
    assert symbols_by_name["add"].kind == SymbolKind.METHOD
    assert symbols_by_name["add"].parent_name == "Calculator"

    assert "compute_async" in symbols_by_name
    assert symbols_by_name["compute_async"].kind == SymbolKind.METHOD
    assert symbols_by_name["compute_async"].parent_name == "Calculator"

    # Check reference resolution
    # 'standalone_func' is locally defined -> resolved!
    # 'sqrt' was imported from math -> resolved!
    call_refs = [r for r in extracted.references if r.kind == ReferenceKind.CALL]
    resolved_names = {r.symbol_name for r in call_refs if r.is_resolved}
    assert "standalone_func" in resolved_names
    assert "sqrt" in resolved_names


def test_python_parser_syntax_error_resilience():
    malformed = "def broken(:\n  return 42"
    parser = PythonParser()
    extracted = parser.parse("broken.py", malformed)
    assert len(extracted.errors) > 0
    assert "SyntaxError" in extracted.errors[0]
    assert len(extracted.symbols) == 0


def test_javascript_typescript_parser():
    ts_code = '''import React, { useState, useEffect as useMount } from 'react';
import { User, UserService } from './services/user';

export const API_TIMEOUT = 5000;

export interface ProfileProps {
    userId: string;
}

export type Status = 'active' | 'inactive';

export class ProfileComponent extends React.Component {
    constructor(props) {
        super(props);
    }

    render() {
        fetchUserData();
        return null;
    }
}

export function fetchUserData() {
    return UserService.get();
}

export const helperArrow = (x: number) => x * 2;
'''
    parser = JavaScriptTypeScriptParser()
    extracted = parser.parse("profile.tsx", ts_code)

    assert not extracted.errors
    assert len(extracted.imports) == 2

    # Check symbols
    symbols_by_name = {s.name: s for s in extracted.symbols}
    assert "API_TIMEOUT" in symbols_by_name
    assert symbols_by_name["API_TIMEOUT"].kind == SymbolKind.CONSTANT

    assert "ProfileProps" in symbols_by_name
    assert symbols_by_name["ProfileProps"].kind == SymbolKind.INTERFACE

    assert "Status" in symbols_by_name
    assert symbols_by_name["Status"].kind == SymbolKind.TYPE_ALIAS

    assert "ProfileComponent" in symbols_by_name
    assert symbols_by_name["ProfileComponent"].kind == SymbolKind.CLASS

    assert "constructor" in symbols_by_name
    assert symbols_by_name["constructor"].kind == SymbolKind.METHOD
    assert symbols_by_name["constructor"].parent_name == "ProfileComponent"

    assert "render" in symbols_by_name
    assert symbols_by_name["render"].kind == SymbolKind.METHOD
    assert symbols_by_name["render"].parent_name == "ProfileComponent"

    assert "fetchUserData" in symbols_by_name
    assert symbols_by_name["fetchUserData"].kind == SymbolKind.FUNCTION

    assert "helperArrow" in symbols_by_name
    assert symbols_by_name["helperArrow"].kind == SymbolKind.FUNCTION

    # Check references
    # fetchUserData call in render -> resolved
    # UserService call in fetchUserData -> resolved (imported from ./services/user)
    refs_by_name = {r.symbol_name: r for r in extracted.references}
    assert "fetchUserData" in refs_by_name
    assert refs_by_name["fetchUserData"].is_resolved is True


def test_parser_registry():
    assert isinstance(get_parser_for_language("python"), PythonParser)
    assert isinstance(get_parser_for_language("typescript"), JavaScriptTypeScriptParser)
    assert isinstance(get_parser_for_language("javascript"), JavaScriptTypeScriptParser)
    assert isinstance(get_parser_for_language("tsx"), JavaScriptTypeScriptParser)
    assert isinstance(get_parser_for_language("jsx"), JavaScriptTypeScriptParser)
    assert get_parser_for_language("unknown") is None
