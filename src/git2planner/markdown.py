"""Lightweight Markdown helpers.

Planner task descriptions are plain text, so issue bodies are flattened into
readable text; task-list items are turned into Planner checklist items and
links are collected to detect attachments.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import urljoin

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_TASK_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\[([ xX])\]\s+(.*\S)\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_SETEXT = re.compile(r"^\s{0,3}(=+|-+)\s*$")
_HRULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(\s*<?([^)\s>]+)>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
_HTML_IMG = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)
_HTML_ALT = re.compile(r"\balt\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)
_HTML_LINK = re.compile(
    r"<a\b[^>]*?\bhref\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL
)
_HTML_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
_HTML_TAG = re.compile(r"</?[A-Za-z][^>]*>")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_ITALIC = re.compile(r"(?<![\w*])([*_])(?=\S)(.+?)(?<=\S)\1(?![\w*])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BARE_URL = re.compile(r"(?<![(<\"'=])\bhttps?://[^\s<>()\"'\]\[]+")
_BLANK_RUN = re.compile(r"\n{3,}")
_TRAILING_PUNCT = ".,;:!?"


@dataclass(frozen=True)
class Link:
    url: str
    text: str
    is_image: bool = False


@dataclass(frozen=True)
class TaskItem:
    title: str
    checked: bool
    line: int = -1
    """Index of the line in the (newline-normalised) Markdown source."""


def _strip_comments(text: str) -> str:
    """Drop HTML comments but keep their line breaks so line numbers stay stable."""
    return _HTML_COMMENT.sub(lambda match: "\n" * match.group(0).count("\n"), text)


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _iter_lines(text: str) -> list[tuple[str, bool]]:
    """Split ``text`` into lines tagged with whether they are inside a code fence."""
    result: list[tuple[str, bool]] = []
    fence: str | None = None
    for line in _normalize_newlines(text).split("\n"):
        match = _FENCE.match(line)
        if fence is None and match:
            fence = match.group(1)[0] * 3
            result.append((line, True))
            continue
        if fence is not None:
            result.append((line, True))
            if line.strip().startswith(fence):
                fence = None
            continue
        result.append((line, False))
    return result


def extract_task_items(markdown: str) -> list[TaskItem]:
    """Return the GitHub-flavoured task-list items (``- [ ] foo``) outside code blocks."""
    items: list[TaskItem] = []
    for index, (line, in_code) in enumerate(_iter_lines(_strip_comments(markdown or ""))):
        if in_code:
            continue
        match = _TASK_ITEM.match(line)
        if match:
            title = _inline_to_text(match.group(2)).strip()
            if title:
                items.append(TaskItem(title=title, checked=match.group(1) in "xX", line=index))
    return items


_CHECKBOX = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)\[([ xX])\]")


def set_task_item_states(markdown: str, states: dict[int, bool]) -> str:
    """Tick or untick the task-list items on the given line indexes."""
    lines = _normalize_newlines(markdown or "").split("\n")
    for index, checked in states.items():
        if 0 <= index < len(lines):
            mark = "x" if checked else " "
            lines[index] = _CHECKBOX.sub(rf"\g<1>[{mark}]", lines[index], count=1)
    return "\n".join(lines)


def append_task_items(markdown: str, items: list[tuple[str, bool]]) -> str:
    """Append task-list items at the end of ``markdown``."""
    if not items:
        return markdown
    block = "\n".join(f"- [{'x' if checked else ' '}] {title}" for title, checked in items)
    text = (markdown or "").rstrip("\n")
    if not text:
        return block
    last_line = text.rsplit("\n", 1)[-1]
    separator = "\n" if _TASK_ITEM.match(last_line) else "\n\n"
    return f"{text}{separator}{block}"


def task_item_lines(markdown: str) -> list[str]:
    """The raw task-list lines of ``markdown`` (used to keep them when replacing a body)."""
    lines = _normalize_newlines(markdown or "").split("\n")
    return [lines[item.line] for item in extract_task_items(markdown)]


def _image_text(alt: str, url: str) -> str:
    label = alt.strip() or url.rsplit("/", 1)[-1] or "image"
    return f"[image: {label}]"


def _link_text(text: str, url: str) -> str:
    text = text.strip()
    if not text or text == url:
        return url
    return f"{text} ({url})"


def _inline_to_text(text: str) -> str:
    def html_img(match: re.Match[str]) -> str:
        alt = _HTML_ALT.search(match.group(0))
        return _image_text(alt.group(1) if alt else "", match.group(1))

    text = _HTML_IMG.sub(html_img, text)
    text = _HTML_LINK.sub(lambda m: _link_text(_HTML_TAG.sub("", m.group(2)), m.group(1)), text)
    text = _HTML_BREAK.sub("\n", text)
    text = _IMAGE.sub(lambda m: _image_text(m.group(1), m.group(2)), text)
    text = _LINK.sub(lambda m: _link_text(m.group(1), m.group(2)), text)
    text = _HTML_TAG.sub("", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _BOLD.sub(r"\2", text)
    text = _STRIKE.sub(r"\1", text)
    text = _ITALIC.sub(r"\2", text)
    return html.unescape(text)


def to_plain_text(markdown: str, *, strip_task_items: bool = False) -> str:
    """Convert Markdown into readable plain text.

    Code blocks are kept verbatim (without the fences), headings and emphasis
    markers are removed, links become ``text (url)`` and images ``[image: alt]``.
    """
    out: list[str] = []
    for line, in_code in _iter_lines(_strip_comments(markdown or "")):
        if in_code:
            if not _FENCE.match(line):
                out.append(line)
            continue
        if strip_task_items and _TASK_ITEM.match(line):
            continue
        if _HRULE.match(line):
            out.append("")
            continue
        if _SETEXT.match(line) and out and out[-1].strip():
            continue
        heading = _HEADING.match(line)
        if heading:
            line = heading.group(1)
        task = _TASK_ITEM.match(line)
        if task:
            mark = "[x]" if task.group(1) in "xX" else "[ ]"
            indent = line[: len(line) - len(line.lstrip())]
            line = f"{indent}{mark} {task.group(2)}"
        else:
            line = _BULLET.sub(lambda m: f"{m.group(1)}• ", line)
        if line.lstrip().startswith(">"):
            line = "| " + line.lstrip()[1:].lstrip()
        out.append(_inline_to_text(line).rstrip())
    text = "\n".join(out).strip("\n")
    return _BLANK_RUN.sub("\n\n", text)


def _clean_url(url: str) -> str:
    url = html.unescape(url.strip())
    while url and url[-1] in _TRAILING_PUNCT:
        url = url[:-1]
    return url


def extract_links(markdown: str, base_url: str | None = None) -> list[Link]:
    """Collect links and images (Markdown, HTML and bare URLs) outside code blocks.

    Relative URLs are resolved against ``base_url`` when given and dropped
    otherwise. The result is de-duplicated, keeping the first occurrence.
    """
    found: list[Link] = []
    for line, in_code in _iter_lines(_strip_comments(markdown or "")):
        if in_code:
            continue
        line = _INLINE_CODE.sub("", line)
        for match in _HTML_IMG.finditer(line):
            alt = _HTML_ALT.search(match.group(0))
            found.append(Link(match.group(1), alt.group(1) if alt else "", True))
        for match in _HTML_LINK.finditer(line):
            found.append(Link(match.group(1), _HTML_TAG.sub("", match.group(2)).strip()))
        stripped = _HTML_IMG.sub(" ", _HTML_LINK.sub(" ", line))
        for match in _IMAGE.finditer(stripped):
            found.append(Link(match.group(2), match.group(1), True))
        for match in _LINK.finditer(stripped):
            found.append(Link(match.group(2), match.group(1)))
        remainder = _LINK.sub(" ", _IMAGE.sub(" ", stripped))
        for match in _BARE_URL.finditer(remainder):
            found.append(Link(match.group(0), ""))

    seen: set[str] = set()
    result: list[Link] = []
    for link in found:
        url = _clean_url(link.url)
        if not url.startswith(("http://", "https://")):
            if base_url is None or url.startswith(("#", "mailto:")) or not url:
                continue
            url = urljoin(base_url, url)
        if url in seen:
            continue
        seen.add(url)
        result.append(Link(url, link.text.strip(), link.is_image))
    return result
