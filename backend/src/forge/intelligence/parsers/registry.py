from __future__ import annotations

from forge.intelligence.parsers.base import BaseParser
from forge.intelligence.parsers.javascript import JavaScriptTypeScriptParser
from forge.intelligence.parsers.python import PythonParser

_PARSERS: dict[str, BaseParser] = {
    "python": PythonParser(),
    "typescript": JavaScriptTypeScriptParser(),
    "javascript": JavaScriptTypeScriptParser(),
    "tsx": JavaScriptTypeScriptParser(),
    "jsx": JavaScriptTypeScriptParser(),
}


def get_parser_for_language(language: str) -> BaseParser | None:
    """Return an appropriate parser instance for the language, or None if unsupported."""
    return _PARSERS.get(language.lower())
