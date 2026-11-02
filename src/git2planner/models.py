"""Provider-agnostic data model for issues."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

_ZERO_TIME_PREFIX = "0001-01-01"
_FRACTION = re.compile(r"\.(\d+)(?=[+-]\d{2}:\d{2}$|$)")


def parse_datetime(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp as returned by the GitHub and Gitea APIs.

    Empty values and Go's zero time (``0001-01-01T00:00:00Z``) yield ``None``.
    Naive timestamps are assumed to be UTC.
    """
    if not value or not isinstance(value, str) or value.startswith(_ZERO_TIME_PREFIX):
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # Graph returns up to 7 fractional digits; Python < 3.11 accepts at most 6.
    text = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_datetime(value: datetime) -> str:
    """Format a timestamp the way Microsoft Graph expects it (UTC, ``Z`` suffix)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Label:
    name: str
    color: str | None = None
    """Hex colour without the leading ``#`` (lowercase), if known."""


@dataclass
class GitUser:
    login: str
    email: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class Milestone:
    title: str
    due_on: datetime | None = None


@dataclass(frozen=True)
class Attachment:
    name: str
    url: str
    size: int | None = None
    content_type: str | None = None


@dataclass
class Issue:
    number: int
    title: str
    body: str
    state: str
    """Either ``"open"`` or ``"closed"``."""
    url: str
    repository: str
    labels: list[Label] = field(default_factory=list)
    assignees: list[GitUser] = field(default_factory=list)
    milestone: Milestone | None = None
    due_date: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    closed_at: datetime | None = None
    attachments: list[Attachment] = field(default_factory=list)
    author: GitUser | None = None
    is_pull_request: bool = False
    state_reason: str | None = None

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    @property
    def label_names(self) -> list[str]:
        return [label.name for label in self.labels]

    @property
    def effective_due(self) -> datetime | None:
        """The issue's own due date, falling back to its milestone's due date."""
        if self.due_date is not None:
            return self.due_date
        if self.milestone is not None:
            return self.milestone.due_on
        return None


@dataclass
class IssueChanges:
    """Changes to write to an issue (``None`` means "leave unchanged")."""

    title: str | None = None
    body: str | None = None
    state: str | None = None
    labels: list[str] | None = None
    assignees: list[str] | None = None
    due_date: datetime | None = None
    clear_due_date: bool = False
    label_colors: dict[str, str] = field(default_factory=dict)
    """Colours (hex without ``#``) for labels the provider may have to create."""

    def fields(self) -> list[str]:
        names = [
            name
            for name in ("title", "body", "state", "labels", "assignees")
            if getattr(self, name) is not None
        ]
        if self.due_date is not None or self.clear_due_date:
            names.append("due_date")
        return names

    def is_empty(self) -> bool:
        return not self.fields()
