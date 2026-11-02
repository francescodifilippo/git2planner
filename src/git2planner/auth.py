"""Microsoft Entra ID (Azure AD) token acquisition for Microsoft Graph."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any

import requests

from .config import GraphConfig
from .errors import AuthError
from .http import USER_AGENT

log = logging.getLogger(__name__)

GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
GITHUB_OIDC_AUDIENCE = "api://AzureADTokenExchange"
_EXPIRY_MARGIN = 120.0


def _post_token(
    session: requests.Session, url: str, data: dict[str, str], timeout: float
) -> dict[str, Any]:
    try:
        response = session.post(url, data=data, timeout=timeout, headers={"User-Agent": USER_AGENT})
    except requests.RequestException as exc:
        raise AuthError(f"cannot reach {url}: {exc}") from exc
    try:
        payload: dict[str, Any] = response.json()
    except ValueError as exc:
        raise AuthError(f"unexpected response from {url}: HTTP {response.status_code}") from exc
    if response.status_code >= 400 or "error" in payload:
        error = payload.get("error", f"HTTP {response.status_code}")
        description = str(payload.get("error_description") or "").split("\r\n")[0]
        raise AuthError(f"{error}: {description}".rstrip(": "))
    return payload


class TokenCredential(ABC):
    """Caches an access token and renews it shortly before it expires."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = 30.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.RLock()

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.get_token()}"}

    def get_token(self) -> str:
        with self._lock:
            if self._token is None or self._clock() >= self._expires_at - _EXPIRY_MARGIN:
                token, expires_in = self._acquire()
                self._token = token
                self._expires_at = self._clock() + expires_in
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None

    @abstractmethod
    def _acquire(self) -> tuple[str, float]:
        """Return a fresh access token and its lifetime in seconds."""


class AccessTokenCredential(TokenCredential):
    """A pre-acquired token, e.g. from ``az account get-access-token``."""

    def __init__(self, token: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._static = token

    def _acquire(self) -> tuple[str, float]:
        return self._static, 365 * 24 * 3600.0


class ClientSecretCredential(TokenCredential):
    """OAuth 2.0 client-credentials grant with a client secret (application permissions)."""

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        *,
        authority: str = "https://login.microsoftonline.com",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.token_url = f"{authority.rstrip('/')}/{tenant_id}/oauth2/v2.0/token"
        self.client_id = client_id
        self.client_secret = client_secret

    def _acquire(self) -> tuple[str, float]:
        payload = _post_token(
            self.session,
            self.token_url,
            {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": GRAPH_DEFAULT_SCOPE,
            },
            self.timeout,
        )
        return str(payload["access_token"]), float(payload.get("expires_in", 3600))


class ClientAssertionCredential(TokenCredential):
    """Client-credentials grant with a federated assertion (workload identity federation)."""

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        assertion: Callable[[], str],
        *,
        authority: str = "https://login.microsoftonline.com",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.token_url = f"{authority.rstrip('/')}/{tenant_id}/oauth2/v2.0/token"
        self.client_id = client_id
        self.assertion = assertion

    def _acquire(self) -> tuple[str, float]:
        payload = _post_token(
            self.session,
            self.token_url,
            {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_assertion_type": ASSERTION_TYPE,
                "client_assertion": self.assertion(),
                "scope": GRAPH_DEFAULT_SCOPE,
            },
            self.timeout,
        )
        return str(payload["access_token"]), float(payload.get("expires_in", 3600))


def file_assertion(path: str) -> Callable[[], str]:
    """Read the assertion from a file each time (Kubernetes rotates projected tokens)."""

    def read() -> str:
        try:
            return Path(os.path.expanduser(path)).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AuthError(f"cannot read federated token file {path}: {exc}") from exc

    return read


def github_oidc_assertion(
    session: requests.Session | None = None,
    audience: str = GITHUB_OIDC_AUDIENCE,
    environ: dict[str, str] | None = None,
) -> Callable[[], str]:
    """Fetch an OIDC token from the GitHub Actions runtime (``permissions: id-token: write``)."""
    http = session or requests.Session()

    def fetch() -> str:
        env = os.environ if environ is None else environ
        url = env.get("ACTIONS_ID_TOKEN_REQUEST_URL")
        token = env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
        if not url or not token:
            raise AuthError(
                "GitHub OIDC is not available: run inside GitHub Actions with "
                "'permissions: id-token: write'"
            )
        try:
            response = http.get(
                url,
                params={"audience": audience},
                headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
                timeout=30,
            )
            response.raise_for_status()
            return str(response.json()["value"])
        except (requests.RequestException, KeyError, ValueError) as exc:
            raise AuthError(f"cannot obtain a GitHub OIDC token: {exc}") from exc

    return fetch


class DeviceCodeCredential(TokenCredential):
    """Delegated authentication via the device-code flow with a refresh-token cache."""

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        scopes: list[str],
        cache_path: str | os.PathLike[str],
        *,
        authority: str = "https://login.microsoftonline.com",
        interactive: bool = False,
        prompt: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        base = f"{authority.rstrip('/')}/{tenant_id}/oauth2/v2.0"
        self.token_url = f"{base}/token"
        self.device_url = f"{base}/devicecode"
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.scopes = list(scopes)
        self.cache_path = Path(os.path.expanduser(os.fspath(cache_path)))
        self.interactive = interactive
        self._prompt = prompt or (lambda message: print(message, file=sys.stderr, flush=True))
        self._sleep = sleep

    # ------------------------------------------------------------------ cache

    def _load_refresh_token(self) -> str | None:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable token cache %s: %s", self.cache_path, exc)
            return None
        if data.get("client_id") != self.client_id or data.get("tenant_id") != self.tenant_id:
            return None
        token = data.get("refresh_token")
        return str(token) if token else None

    def _save(self, payload: dict[str, Any]) -> None:
        refresh = payload.get("refresh_token")
        if not refresh:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(
            {
                "tenant_id": self.tenant_id,
                "client_id": self.client_id,
                "refresh_token": refresh,
                "saved_at": int(self._clock()),
            }
        )
        tmp = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp, self.cache_path)

    def clear_cache(self) -> bool:
        try:
            self.cache_path.unlink()
        except FileNotFoundError:
            return False
        return True

    # ------------------------------------------------------------------ flows

    def _refresh(self, refresh_token: str) -> dict[str, Any]:
        return _post_token(
            self.session,
            self.token_url,
            {
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "refresh_token": refresh_token,
                "scope": " ".join(self.scopes),
            },
            self.timeout,
        )

    def login(self) -> dict[str, Any]:
        """Run the interactive device-code flow and persist the refresh token."""
        start = _post_token(
            self.session,
            self.device_url,
            {"client_id": self.client_id, "scope": " ".join(self.scopes)},
            self.timeout,
        )
        self._prompt(str(start.get("message") or start))
        interval = float(start.get("interval", 5))
        deadline = self._clock() + float(start.get("expires_in", 900))
        while self._clock() < deadline:
            self._sleep(interval)
            try:
                payload = _post_token(
                    self.session,
                    self.token_url,
                    {
                        "grant_type": DEVICE_CODE_GRANT,
                        "client_id": self.client_id,
                        "device_code": str(start["device_code"]),
                    },
                    self.timeout,
                )
            except AuthError as exc:
                message = str(exc)
                if message.startswith("authorization_pending"):
                    continue
                if message.startswith("slow_down"):
                    interval += 5
                    continue
                raise
            self._save(payload)
            with self._lock:
                self._token = str(payload["access_token"])
                self._expires_at = self._clock() + float(payload.get("expires_in", 3600))
            return payload
        raise AuthError("device code expired before the sign-in was completed")

    def _acquire(self) -> tuple[str, float]:
        refresh = self._load_refresh_token()
        if refresh:
            try:
                payload = self._refresh(refresh)
            except AuthError as exc:
                log.warning("cached sign-in is no longer valid: %s", exc)
            else:
                self._save(payload)
                return str(payload["access_token"]), float(payload.get("expires_in", 3600))
        if not self.interactive:
            raise AuthError("not signed in to Microsoft 365: run 'git2planner login' first")
        payload = self.login()
        return str(payload["access_token"]), float(payload.get("expires_in", 3600))


def create_credential(
    config: GraphConfig, *, interactive: bool = False, session: requests.Session | None = None
) -> TokenCredential:
    """Build the credential selected by ``graph.auth``."""
    method = config.resolved_auth()
    common: dict[str, Any] = {"timeout": config.timeout}
    if session is not None:
        common["session"] = session
    if method == "access_token":
        assert config.access_token
        return AccessTokenCredential(config.access_token, **common)
    assert config.client_id
    assert config.tenant_id
    if method == "client_secret":
        assert config.client_secret
        return ClientSecretCredential(
            config.tenant_id,
            config.client_id,
            config.client_secret,
            authority=config.authority,
            **common,
        )
    if method == "federated":
        assert config.client_assertion_file
        return ClientAssertionCredential(
            config.tenant_id,
            config.client_id,
            file_assertion(config.client_assertion_file),
            authority=config.authority,
            **common,
        )
    if method == "github_oidc":
        return ClientAssertionCredential(
            config.tenant_id,
            config.client_id,
            github_oidc_assertion(session),
            authority=config.authority,
            **common,
        )
    return DeviceCodeCredential(
        config.tenant_id,
        config.client_id,
        config.scopes,
        config.token_cache,
        authority=config.authority,
        interactive=interactive,
        **common,
    )
