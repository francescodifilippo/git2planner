"""Git hosting providers."""

from __future__ import annotations

from ..config import SourceConfig
from ..errors import ConfigError
from .base import IssueProvider
from .gitea import ForgejoProvider, GiteaProvider
from .github import GitHubProvider
from .gitlab import GitLabProvider

PROVIDER_CLASSES: dict[str, type[IssueProvider]] = {
    GitHubProvider.kind: GitHubProvider,
    GiteaProvider.kind: GiteaProvider,
    ForgejoProvider.kind: ForgejoProvider,
    GitLabProvider.kind: GitLabProvider,
}


def create_provider(config: SourceConfig) -> IssueProvider:
    """Instantiate the provider described by ``config``."""
    try:
        cls = PROVIDER_CLASSES[config.provider]
    except KeyError as exc:
        raise ConfigError(f"unsupported provider {config.provider!r}") from exc
    return cls(config)


__all__ = [
    "PROVIDER_CLASSES",
    "ForgejoProvider",
    "GitHubProvider",
    "GitLabProvider",
    "GiteaProvider",
    "IssueProvider",
    "create_provider",
]
