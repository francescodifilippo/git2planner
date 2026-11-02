"""A small HTTP layer on top of :mod:`requests` with retries and pagination."""

from __future__ import annotations

import email.utils
import logging
import random
import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Protocol

import requests

from . import __version__
from .errors import (
    HTTPError,
    NotFoundError,
    PermissionDeniedError,
    PreconditionFailedError,
    RateLimitError,
)

log = logging.getLogger(__name__)

USER_AGENT = f"git2planner/{__version__} (+https://github.com/francescodifilippo/git2planner)"

#: Status codes that are always worth retrying for idempotent requests.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
#: For non-idempotent requests only retry when the server certainly did not act.
RETRY_STATUSES_UNSAFE = frozenset({429, 503})
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE", "PATCH"})


class AuthProvider(Protocol):
    """Supplies authentication headers, possibly refreshing them on demand."""

    def headers(self) -> dict[str, str]: ...

    def invalidate(self) -> None: ...


class StaticHeaderAuth:
    """Authentication through a fixed set of headers (API tokens)."""

    def __init__(self, headers: Mapping[str, str]) -> None:
        self._headers = dict(headers)

    def headers(self) -> dict[str, str]:
        return dict(self._headers)

    def invalidate(self) -> None:
        """Static credentials cannot be refreshed."""


def _parse_retry_after(value: str | None, now: float) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, parsed.timestamp() - now)


def _error_message(response: requests.Response) -> tuple[str, str | None]:
    """Extract a human readable message and error code from an error response."""
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return (text[:300] or response.reason or "no details"), None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or payload), error.get("code")
        if isinstance(error, str):
            description = payload.get("error_description") or payload.get("message")
            return (str(description) if description else error), error
        message = payload.get("message")
        if message:
            errors = payload.get("errors")
            if errors:
                return f"{message} ({errors})", None
            return str(message), None
    return str(payload)[:300], None


class HttpClient:
    """Thin wrapper around a :class:`requests.Session`.

    * prefixes relative paths with ``base_url``;
    * injects authentication headers and refreshes them once on HTTP 401;
    * retries transient failures with exponential back-off, honouring
      ``Retry-After`` and the GitHub/GitLab rate-limit headers;
    * converts error responses into :class:`~git2planner.errors.HTTPError`
      subclasses.
    """

    def __init__(
        self,
        base_url: str,
        *,
        auth: AuthProvider | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        max_retries: int = 5,
        backoff: float = 1.0,
        max_wait: float = 900.0,
        verify: bool | str = True,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.default_headers = {"User-Agent": USER_AGENT, **(headers or {})}
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.max_wait = max_wait
        self.verify = verify
        self.session = session or requests.Session()
        self._sleep = sleep
        self._clock = clock

    def url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        headers: Mapping[str, str] | None = None,
        stream: bool = False,
        authenticated: bool = True,
        raise_for_status: bool = True,
    ) -> requests.Response:
        method = method.upper()
        url = self.url(path)
        attempt = 0
        refreshed = False
        while True:
            request_headers = dict(self.default_headers)
            if authenticated and self.auth is not None:
                request_headers.update(self.auth.headers())
            if headers:
                request_headers.update(headers)
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    data=data,
                    headers=request_headers,
                    timeout=self.timeout,
                    stream=stream,
                    verify=self.verify,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if method in IDEMPOTENT_METHODS and attempt < self.max_retries:
                    delay = self._backoff_delay(attempt)
                    attempt += 1
                    log.warning("%s %s failed (%s), retrying in %.1fs", method, url, exc, delay)
                    self._sleep(delay)
                    continue
                raise HTTPError(f"{method} {url} failed: {exc}", url=url) from exc

            if (
                response.status_code == 401
                and authenticated
                and self.auth is not None
                and not refreshed
            ):
                refreshed = True
                self.auth.invalidate()
                response.close()
                continue

            wait = self._retry_delay(method, response, attempt)
            if wait is not None:
                attempt += 1
                log.warning(
                    "%s %s returned HTTP %s, retrying in %.1fs (attempt %d/%d)",
                    method,
                    url,
                    response.status_code,
                    wait,
                    attempt,
                    self.max_retries,
                )
                response.close()
                self._sleep(wait)
                continue

            if raise_for_status and response.status_code >= 400:
                raise self.to_error(method, url, response)
            return response

    def get_json(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params).json()

    def paginate(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        items_key: str | None = None,
    ) -> Iterator[Any]:
        """Iterate over a collection paginated with RFC 5988 ``Link`` headers."""
        url: str | None = self.url(path)
        query: Mapping[str, Any] | None = params
        while url:
            response = self.request("GET", url, params=query)
            payload = response.json()
            items = payload.get(items_key, []) if items_key else payload
            yield from items or []
            url = response.links.get("next", {}).get("url")
            query = None

    def paginate_odata(
        self, path: str, *, params: Mapping[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        """Iterate over a Microsoft Graph collection following ``@odata.nextLink``."""
        url: str | None = self.url(path)
        query: Mapping[str, Any] | None = params
        while url:
            payload = self.request("GET", url, params=query).json()
            yield from payload.get("value", [])
            url = payload.get("@odata.nextLink")
            query = None

    def _backoff_delay(self, attempt: int) -> float:
        base: float = self.backoff * (2**attempt)
        return min(base + random.uniform(0, self.backoff), self.max_wait)

    def _retry_delay(self, method: str, response: requests.Response, attempt: int) -> float | None:
        if attempt >= self.max_retries:
            return None
        status = response.status_code
        headers = response.headers
        rate_limited = status == 429 or (
            status == 403
            and (headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers)
        )
        retryable = RETRY_STATUSES if method in IDEMPOTENT_METHODS else RETRY_STATUSES_UNSAFE
        if not rate_limited and status not in retryable:
            return None

        now = self._clock()
        delay = _parse_retry_after(headers.get("retry-after"), now)
        if delay is None and rate_limited:
            reset = headers.get("x-ratelimit-reset") or headers.get("ratelimit-reset")
            if reset and reset.isdigit():
                delay = max(0.0, float(reset) - now) + 1.0
        if delay is None:
            delay = self._backoff_delay(attempt)
        if delay > self.max_wait:
            log.error(
                "%s %s: server asked to wait %.0fs which exceeds the limit of %.0fs",
                method,
                response.url,
                delay,
                self.max_wait,
            )
            return None
        return delay

    @staticmethod
    def to_error(method: str, url: str, response: requests.Response) -> HTTPError:
        message, code = _error_message(response)
        status = response.status_code
        text = f"{method} {url} -> HTTP {status}: {message}"
        error_type: type[HTTPError]
        if status in (404, 410):
            error_type = NotFoundError
        elif status in (401, 403):
            error_type = PermissionDeniedError
        elif status in (409, 412):
            error_type = PreconditionFailedError
        elif status == 429:
            error_type = RateLimitError
        else:
            error_type = HTTPError
        return error_type(text, status=status, code=code, url=url)
