from __future__ import annotations

import hashlib
import html
import json
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

SKIP_TAGS = {
    "script",
    "style",
    "noscript",
    "svg",
    "canvas",
    "nav",
    "footer",
    "aside",
    "header",
    "form",
}

HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "main",
    "blockquote",
    "hr",
    "table",
    "tr",
    "ul",
    "ol",
    "li",
    *HEADING_TAGS,
}

IGNORE_CLASS_ID_PATTERNS = [
    re.compile(r"cookie", re.IGNORECASE),
    re.compile(r"consent", re.IGNORECASE),
    re.compile(r"banner", re.IGNORECASE),
    re.compile(r"navbar", re.IGNORECASE),
    re.compile(r"navigation", re.IGNORECASE),
    re.compile(r"sidebar", re.IGNORECASE),
    re.compile(r"footer", re.IGNORECASE),
    re.compile(r"menu", re.IGNORECASE),
    re.compile(r"ad[-_]", re.IGNORECASE),
    re.compile(r"advertisement", re.IGNORECASE),
]


class ExtractedContent:
    def __init__(
        self,
        *,
        title: str = "",
        canonical_url: str | None = None,
        text: str = "",
        headings: list[str] | None = None,
        code_blocks: list[str] | None = None,
        tables: list[str] | None = None,
        links: list[str] | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        self.title = title.strip()
        self.canonical_url = canonical_url
        self.text = text.strip()
        self.headings = headings or []
        self.code_blocks = code_blocks or []
        self.tables = tables or []
        self.links = links or []
        self.warnings = warnings or []
        self.content_hash = hashlib.sha256(self.text.encode("utf-8")).hexdigest() if self.text else ""


class HTMLDocumentationParser(HTMLParser):
    """Robust HTML parser that preserves documentation structure, code, hierarchy, and excludes chrome."""

    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title = ""
        self.canonical_url: str | None = None
        self.headings: list[str] = []
        self.code_blocks: list[str] = []
        self.links: list[str] = []
        self.warnings: list[str] = []

        self._in_title = False
        self._skip_tag_stack: list[str] = []
        self._in_code = False
        self._in_pre = False
        self._current_heading_level = 0
        self._current_heading_text: list[str] = []
        self._current_code_text: list[str] = []
        self._in_table = False
        self._table_cells: list[str] = []
        self._output_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        attr_dict = {k.lower(): v for k, v in attrs if v is not None}

        # If a body or block tag starts while _in_title is True (malformed unclosed <title>), close title
        if self._in_title and (tag_lower in BLOCK_TAGS or tag_lower == "body"):
            self._in_title = False

        # Check canonical URL
        if tag_lower == "link" and attr_dict.get("rel") == "canonical":
            href = attr_dict.get("href")
            if href:
                self.canonical_url = urljoin(self.base_url, href)

        if tag_lower == "title":
            self._in_title = True
            return

        # Check for ignored tags or classes
        tag_class = attr_dict.get("class", "")
        tag_id = attr_dict.get("id", "")
        combined_attrs = f"{tag_class} {tag_id}"
        should_ignore = tag_lower in SKIP_TAGS or any(p.search(combined_attrs) for p in IGNORE_CLASS_ID_PATTERNS)

        if self._skip_tag_stack:
            self._skip_tag_stack.append(tag_lower)
            return

        if should_ignore:
            self._skip_tag_stack.append(tag_lower)
            return

        if self._current_heading_level > 0 and tag_lower in BLOCK_TAGS:
            heading_str = "".join(self._current_heading_text).strip()
            if heading_str:
                self.headings.append(heading_str)
                prefix = "#" * self._current_heading_level
                self._output_chunks.append(f"\n\n{prefix} {heading_str}\n\n")
            self._current_heading_level = 0
            self._current_heading_text = []

        if tag_lower in HEADING_TAGS:
            self._current_heading_level = int(tag_lower[1])
            self._current_heading_text = []

        elif tag_lower == "pre":
            self._in_pre = True
            self._current_code_text = []

        elif tag_lower == "code":
            if self._in_pre:
                self._in_code = True

        elif tag_lower == "a":
            href = attr_dict.get("href")
            if href and not href.startswith(("#", "javascript:", "mailto:")):
                abs_link = urljoin(self.base_url, href)
                self.links.append(abs_link)

        elif tag_lower == "table":
            self._in_table = True
            self._output_chunks.append("\n\n")

        elif tag_lower == "tr":
            self._table_cells = []

        elif tag_lower in {"th", "td"}:
            pass

        elif tag_lower in BLOCK_TAGS:
            self._output_chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()

        if tag_lower == "title":
            self._in_title = False
            return

        if self._skip_tag_stack:
            self._skip_tag_stack.pop()
            return

        if tag_lower in HEADING_TAGS:
            heading_str = "".join(self._current_heading_text).strip()
            if heading_str:
                self.headings.append(heading_str)
                prefix = "#" * self._current_heading_level
                self._output_chunks.append(f"\n\n{prefix} {heading_str}\n\n")
            self._current_heading_level = 0
            self._current_heading_text = []

        elif tag_lower == "pre":
            if self._in_pre:
                code_str = "".join(self._current_code_text)
                if code_str.strip():
                    self.code_blocks.append(code_str)
                    self._output_chunks.append(f"\n```\n{code_str}\n```\n")
                self._in_pre = False
                self._in_code = False
                self._current_code_text = []

        elif tag_lower == "code":
            self._in_code = False

        elif tag_lower == "tr":
            if self._table_cells:
                self._output_chunks.append("| " + " | ".join(self._table_cells) + " |\n")
            self._table_cells = []

        elif tag_lower == "table":
            self._in_table = False
            self._output_chunks.append("\n")

        elif tag_lower in BLOCK_TAGS:
            self._output_chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return

        if self._skip_tag_stack:
            return

        if self._current_heading_level > 0:
            self._current_heading_text.append(data)

        if self._in_pre or self._in_code:
            self._current_code_text.append(data)
            return

        if self._in_table:
            clean = data.strip().replace("|", "\\|")
            if clean:
                self._table_cells.append(clean)
            return

        self._output_chunks.append(data)

    def close(self) -> None:
        if self._current_heading_level > 0:
            heading_str = "".join(self._current_heading_text).strip()
            if heading_str:
                self.headings.append(heading_str)
            self._current_heading_level = 0
            self._current_heading_text = []
        super().close()


def extract_html(html_text: str, base_url: str = "") -> ExtractedContent:
    """Parse HTML, removing boilerplate and preserving code, tables, headings, and hierarchy."""
    # Pre-sanitize unclosed <title> tags followed by body/block elements
    cleaned_html = re.sub(
        r"(<title[^>]*>[^<]*?)(<(?:body|h[1-6]|main|div|p|section)\b)",
        r"\1</title>\2",
        html_text,
        flags=re.IGNORECASE,
    )
    parser = HTMLDocumentationParser(base_url=base_url)
    try:
        parser.feed(cleaned_html)
        parser.close()
    except Exception as exc:
        parser.warnings.append(f"HTML parsing warning: {exc}")

    raw_text = "".join(parser._output_chunks)
    # Normalize excessive newlines and whitespace outside code blocks
    lines = raw_text.splitlines()
    normalized_lines: list[str] = []
    in_code = False

    for line in lines:
        if line.strip().startswith("```"):
            in_code = not in_code
            normalized_lines.append(line)
            continue

        if in_code:
            normalized_lines.append(line)
        else:
            stripped = re.sub(r"[ \t]+", " ", line).strip()
            if stripped or (normalized_lines and normalized_lines[-1] != ""):
                normalized_lines.append(stripped)

    # Collapse multiple blank lines
    collapsed: list[str] = []
    for line in normalized_lines:
        if line == "" and collapsed and collapsed[-1] == "":
            continue
        collapsed.append(line)

    clean_text = "\n".join(collapsed).strip()

    # Fallback title from first H1 if <title> was empty
    title = parser.title.strip()
    if "<" in title:
        title = title.split("<")[0].strip()
    if not title and parser.headings:
        title = parser.headings[0]

    return ExtractedContent(
        title=title,
        canonical_url=parser.canonical_url,
        text=clean_text,
        headings=parser.headings,
        code_blocks=parser.code_blocks,
        links=list(dict.fromkeys(parser.links)),
        warnings=parser.warnings,
    )


def extract_markdown(md_text: str, base_url: str = "") -> ExtractedContent:
    """Extract headings, code blocks, links, and content from Markdown documents."""
    headings: list[str] = []
    code_blocks: list[str] = []
    links: list[str] = []
    warnings: list[str] = []

    # Extract headings (# Heading)
    for m in re.finditer(r"^(#{1,6})\s+(.+)$", md_text, re.MULTILINE):
        headings.append(m.group(2).strip())

    # Extract fenced code blocks
    for m in re.finditer(r"```[a-zA-Z0-9_\-\.]*\n(.*?)\n```", md_text, re.DOTALL):
        code_blocks.append(m.group(1))

    # Extract links [text](url)
    for m in re.finditer(r"\[([^\]]+)\]\(([^)]+)\)", md_text):
        link_target = m.group(2).split()[0]
        if not link_target.startswith(("#", "javascript:", "mailto:")):
            links.append(urljoin(base_url, link_target))

    title = headings[0] if headings else ""

    return ExtractedContent(
        title=title,
        canonical_url=None,
        text=md_text.strip(),
        headings=headings,
        code_blocks=code_blocks,
        links=list(dict.fromkeys(links)),
        warnings=warnings,
    )


def extract_json_doc(json_text: str, base_url: str = "") -> ExtractedContent:
    """Extract documentation text from structured JSON API / doc responses."""
    warnings: list[str] = []
    try:
        data = json.loads(json_text)
    except Exception as exc:
        warnings.append(f"JSON parsing error: {exc}")
        return ExtractedContent(text=json_text, warnings=warnings)

    title = ""
    text_parts: list[str] = []
    headings: list[str] = []

    def walk(obj: Any, prefix: str = "") -> None:
        nonlocal title
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k.lower() in {"title", "name", "summary"} and isinstance(v, str) and not title:
                    title = v
                if k.lower() in {"heading", "section", "module", "class", "function"} and isinstance(v, str):
                    headings.append(v)
                walk(v, prefix=f"{prefix}.{k}" if prefix else k)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, prefix=prefix)
        elif isinstance(obj, str):
            clean = obj.strip()
            if clean:
                text_parts.append(f"{prefix}: {clean}" if prefix else clean)

    walk(data)

    return ExtractedContent(
        title=title,
        text="\n".join(text_parts).strip(),
        headings=headings,
        warnings=warnings,
    )


def extract_content(content: str, content_type: str, base_url: str = "") -> ExtractedContent:
    """Unified entry point to extract and normalize document content based on content type."""
    mime = content_type.split(";")[0].strip().lower() if content_type else "text/html"

    if "markdown" in mime:
        return extract_markdown(content, base_url=base_url)
    elif "json" in mime:
        return extract_json_doc(content, base_url=base_url)
    elif "plain" in mime:
        # Plain text
        lines = [line.strip() for line in content.splitlines()]
        clean = "\n".join(line for line in lines if line)
        title = lines[0] if lines else ""
        return ExtractedContent(title=title, text=clean)
    else:
        # Default HTML
        return extract_html(content, base_url=base_url)
