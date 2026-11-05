"""Common interface for Git hosting providers."""

from __future__ import annotations

import logging
import posixpath
import re
from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import datetime
from typing import Any, ClassVar
from urllib.parse import unquote, urlparse

from ..attachments import filename_from_url
from ..config import BoardConfig, SourceConfig
from ..errors import AttachmentError, ConfigError, HTTPError
from ..http import HttpClient
from ..markdown import extract_links
from ..models import Attachment, GitUser, Issue, IssueChanges

log = logging.getLogger(__name__)

_GENERIC_ALT = re.compile(r"^(image|img|screenshot|untitled|attachment)$", re.IGNORECASE)


class Board(ABC):
    """A project board (kanban) whose columns are mirrored as Planner buckets."""

    #: Whether an issue can be taken off the board's columns (label-based boards).
    can_clear: bool = False

    def __init__(self, title: str, columns: list[str]) -> None:
        self.title = title
        self.columns = columns

    @abstractmethod
    def column_of(self, issue: Issue) -> str | None:
        """The column holding ``issue``, or ``None`` when it is not on the board."""

    @abstractmethod
    def move(self, issue: Issue, column: str | None) -> Issue:
        """Put ``issue`` into ``column`` (adding it to the board if needed)."""


class IssueProvider(ABC):
    """Reads issues from a Git hosting service."""

    kind: ClassVar[str]
    display_name: ClassVar[str]
    #: Whether issues have their own due date that can be written.
    supports_due_date: ClassVar[bool] = True

    def __init__(self, config: SourceConfig, http: HttpClient | None = None) -> None:
        self.config = config
        self.repository = config.repository
        self.http = http if http is not None else self._create_http()
        self._users: dict[str, GitUser] = {}

    # ------------------------------------------------------------------ abstract API

    @property
    @abstractmethod
    def web_url(self) -> str:
        """Base URL of the web interface (used for links and attachment detection)."""

    @abstractmethod
    def _create_http(self) -> HttpClient: ...

    @abstractmethod
    def iter_issues(
        self,
        state: str = "all",
        since: datetime | None = None,
        *,
        include_pull_requests: bool = False,
    ) -> Iterator[Issue]:
        """Yield issues in ``state`` (``open``, ``closed`` or ``all``) updated after ``since``."""

    @abstractmethod
    def get_issue(self, number: int) -> Issue | None:
        """Return a single issue, or ``None`` if it was deleted or moved elsewhere."""

    @abstractmethod
    def _fetch_user(self, login: str) -> GitUser | None: ...

    @abstractmethod
    def check_access(self) -> str:
        """Verify that the repository is readable; return a short description of it."""

    @abstractmethod
    def is_attachment_url(self, url: str) -> bool:
        """Whether ``url`` points to a file uploaded to this provider."""

    def load_board(self, config: BoardConfig) -> Board:
        """Read the project board used by the ``board`` bucket strategy."""
        raise ConfigError(
            f"{self.display_name} has no project board API; use the 'label' bucket strategy"
        )

    @abstractmethod
    def create_issue(self, changes: IssueChanges) -> Issue:
        """Open a new issue from ``changes`` (``title`` is required)."""

    @abstractmethod
    def update_issue(self, number: int, changes: IssueChanges) -> Issue:
        """Apply ``changes`` to an issue and return its new version."""

    # ------------------------------------------------------------------ shared helpers

    @property
    def project_web_url(self) -> str:
        return f"{self.web_url}/{self.repository}"

    def get_user(self, login: str) -> GitUser:
        """Return profile details (name, public e-mail) for ``login``, cached."""
        key = login.lower()
        if key not in self._users:
            user: GitUser | None = None
            try:
                user = self._fetch_user(login)
            except HTTPError as exc:
                log.debug("cannot fetch %s user %s: %s", self.display_name, login, exc)
            self._users[key] = user or GitUser(login=login)
        return self._users[key]

    def body_attachments(self, body: str) -> list[Attachment]:
        """Find uploaded files referenced from an issue body."""
        attachments: list[Attachment] = []
        for link in extract_links(body, base_url=self.project_web_url + "/"):
            if not self.is_attachment_url(link.url):
                continue
            name = filename_from_url(link.url)
            text = link.text.strip()
            if (
                text
                and not _GENERIC_ALT.match(text)
                and not text.startswith(("http://", "https://"))
            ):
                _, url_ext = posixpath.splitext(name)
                _, text_ext = posixpath.splitext(text)
                name = text if text_ext or not url_ext else f"{text}{url_ext}"
            attachments.append(Attachment(name=name, url=link.url))
        return attachments

    @staticmethod
    def merge_attachments(*groups: list[Attachment]) -> list[Attachment]:
        seen: set[str] = set()
        merged: list[Attachment] = []
        for group in groups:
            for attachment in group:
                if attachment.url not in seen:
                    seen.add(attachment.url)
                    merged.append(attachment)
        return merged

    def trusted_host(self, url: str) -> bool:
        """Whether the provider token may be sent to ``url``."""
        host = urlparse(url).hostname
        return bool(host) and host in {
            urlparse(self.web_url).hostname,
            urlparse(self.http.base_url).hostname,
        }

    def download_url(self, attachment: Attachment) -> str:
        """URL used to download ``attachment`` (providers may prefer an API route)."""
        return attachment.url

    def download(self, attachment: Attachment, max_bytes: int) -> tuple[bytes, str | None]:
        """Download an attachment, refusing files larger than ``max_bytes``."""
        url = self.download_url(attachment)
        try:
            response = self.http.request(
                "GET", url, stream=True, authenticated=self.trusted_host(url)
            )
        except HTTPError as exc:
            raise AttachmentError(f"cannot download {attachment.url}: {exc}") from exc
        try:
            length = response.headers.get("content-length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise AttachmentError(
                    f"{attachment.name} is {int(length) / 1048576:.1f} MiB, "
                    f"larger than the {max_bytes / 1048576:.1f} MiB limit"
                )
            content_type = response.headers.get("content-type")
            if content_type and content_type.startswith("text/html"):
                raise AttachmentError(
                    f"{attachment.url} returned an HTML page instead of a file "
                    "(the file may require a browser login)"
                )
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=65536):
                total += len(chunk)
                if total > max_bytes:
                    raise AttachmentError(
                        f"{attachment.name} exceeds the {max_bytes / 1048576:.1f} MiB limit"
                    )
                chunks.append(chunk)
            return b"".join(chunks), content_type
        finally:
            response.close()

    @staticmethod
    def label_color(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        color = value.strip().lstrip("#").lower()
        if len(color) == 3:
            color = "".join(ch * 2 for ch in color)
        return color if re.fullmatch(r"[0-9a-f]{6}", color) else None

    @staticmethod
    def unquote_path(url: str) -> str:
        return unquote(urlparse(url).path)
