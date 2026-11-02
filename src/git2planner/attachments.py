"""Helpers for Planner task references (links) and attachment files."""

from __future__ import annotations

import mimetypes
import posixpath
import re
from urllib.parse import unquote, urlparse

_REFERENCE_TYPES = {
    ".doc": "Word",
    ".docx": "Word",
    ".docm": "Word",
    ".odt": "Word",
    ".rtf": "Word",
    ".xls": "Excel",
    ".xlsx": "Excel",
    ".xlsm": "Excel",
    ".csv": "Excel",
    ".ods": "Excel",
    ".ppt": "PowerPoint",
    ".pptx": "PowerPoint",
    ".odp": "PowerPoint",
    ".pdf": "Pdf",
    ".one": "OneNote",
    ".vsd": "Visio",
    ".vsdx": "Visio",
    ".mpp": "Project",
}
_INVALID_FILENAME_CHARS = re.compile(r'[\x00-\x1f"*:<>?/\\|#%]')
_MAX_FILENAME = 200


def reference_key(url: str) -> str:
    """Encode a URL as a Planner ``references`` property name.

    Graph requires ``%``, ``.``, ``:``, ``@`` and ``#`` to be percent-encoded
    in the keys of the ``plannerExternalReferences`` open type.
    """
    return (
        url.replace("%", "%25")
        .replace(".", "%2E")
        .replace(":", "%3A")
        .replace("@", "%40")
        .replace("#", "%23")
    )


def decode_reference_key(key: str) -> str:
    """Return the URL behind a ``references`` property name."""
    return unquote(key)


def reference_type(name: str) -> str:
    """Map a file name to a ``plannerExternalReference.type`` value."""
    extension = posixpath.splitext(urlparse(name).path or name)[1].lower()
    return _REFERENCE_TYPES.get(extension, "Other")


def filename_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    return unquote(posixpath.basename(path)) or "attachment"


def sanitize_filename(name: str, *, content_type: str | None = None) -> str:
    """Make ``name`` safe for SharePoint / OneDrive and give it an extension if possible."""
    cleaned = _INVALID_FILENAME_CHARS.sub("_", name).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned) or "attachment"
    stem, extension = posixpath.splitext(cleaned)
    if not extension and content_type:
        guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
        if guessed:
            extension = ".jpg" if guessed == ".jpe" else guessed
    if len(stem) + len(extension) > _MAX_FILENAME:
        stem = stem[: _MAX_FILENAME - len(extension)]
    return f"{stem}{extension}"


def sanitize_folder(path: str) -> str:
    """Sanitise every segment of a ``/``-separated drive folder path."""
    segments = [sanitize_filename(segment) for segment in path.split("/") if segment.strip()]
    return "/".join(segments)
