"""Exception hierarchy used across git2planner."""

from __future__ import annotations


class Git2PlannerError(Exception):
    """Base class for every error raised by git2planner."""


class ConfigError(Git2PlannerError):
    """The configuration file is missing, malformed or inconsistent."""


class AuthError(Git2PlannerError):
    """Authentication against Microsoft Entra ID failed."""


class StateError(Git2PlannerError):
    """The state file cannot be read, written or locked."""


class AttachmentError(Git2PlannerError):
    """An attachment could not be downloaded or uploaded."""


class HTTPError(Git2PlannerError):
    """An HTTP request failed with an unexpected status code."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.url = url


class NotFoundError(HTTPError):
    """HTTP 404 / 410."""


class PermissionDeniedError(HTTPError):
    """HTTP 401 / 403."""


class PreconditionFailedError(HTTPError):
    """HTTP 409 / 412: the resource changed since its ETag was read."""


class RateLimitError(HTTPError):
    """HTTP 429 that could not be absorbed by retrying."""
