from __future__ import annotations

from abc import ABC, abstractmethod

from forge.intelligence.models import ExtractedFileSymbols


class BaseParser(ABC):
    """Abstract base class for language symbol and reference parsers."""

    @abstractmethod
    def parse(self, rel_path: str, content: str) -> ExtractedFileSymbols:
        """Extract symbols, imports, and references from file content without executing it."""
        raise NotImplementedError
