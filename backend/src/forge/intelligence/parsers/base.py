from __future__ import annotations

from abc import ABC, abstractmethod

from forge.intelligence.models import ExtractedFileSymbols


class BaseParser(ABC):
    """Abstract base class for language symbol and reference parsers.

    Note on Reference Resolution:
    Reference resolution in Forge is an approximate syntactic navigation aid based
    on intra-file definitions and imported identifier names. It does not construct a
    compiler-grade cross-file type graph or resolve dynamic attributes, aliases,
    overloads, or complex type-level expressions.
    """

    @abstractmethod
    def parse(self, rel_path: str, content: str) -> ExtractedFileSymbols:
        """Extract symbols, imports, and approximate references from file content without executing it."""
        raise NotImplementedError
