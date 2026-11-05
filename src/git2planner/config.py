"""Loading and validation of the YAML configuration file."""

from __future__ import annotations

import copy
import os
import re
import string
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError

PROVIDERS = ("github", "gitea", "forgejo", "gitlab")
ISSUE_STATES = ("open", "closed", "all")
BUCKET_STRATEGIES = ("fixed", "label", "milestone", "board")
CATEGORY_MODES = ("auto", "explicit", "off")
ATTACHMENT_MODES = ("link", "upload", "off")
ON_CLOSE = ("complete", "delete", "ignore")
ON_MISSING = ("keep", "complete", "delete")
PREVIEW_TYPES = ("automatic", "noPreview", "checklist", "description", "reference")
DIRECTIONS = ("git-to-planner", "planner-to-git", "both")
DIRECTION_ALIASES = {
    "push": "git-to-planner",
    "pull": "planner-to-git",
    "bidirectional": "both",
    "two-way": "both",
}
CONFLICT_POLICIES = ("git", "planner")
PULL_FIELDS = (
    "state",
    "title",
    "assignees",
    "labels",
    "bucket",
    "due_date",
    "checklist",
    "description",
)
DEFAULT_PULL_FIELDS = ("state", "title", "assignees", "labels", "bucket", "due_date", "checklist")
AUTH_METHODS = (
    "auto",
    "client_secret",
    "federated",
    "github_oidc",
    "device_code",
    "access_token",
)
PRIORITY_NAMES = {"urgent": 1, "important": 3, "medium": 5, "low": 9}

DEFAULT_CONFIG_FILE = "git2planner.yml"
DEFAULT_STATE_FILE = ".git2planner/state.json"
DEFAULT_TOKEN_CACHE = "~/.cache/git2planner/token.json"
DEFAULT_DELEGATED_SCOPES = (
    "https://graph.microsoft.com/Tasks.ReadWrite",
    "https://graph.microsoft.com/GroupMember.Read.All",
    "https://graph.microsoft.com/User.ReadBasic.All",
    "https://graph.microsoft.com/Files.ReadWrite.All",
    "offline_access",
)
TITLE_FIELDS = ("number", "title", "repository", "owner", "repo", "provider", "state")
FOLDER_FIELDS = ("number", "repository", "owner", "repo", "provider")

#: Environment variables used when the corresponding setting is omitted.
TOKEN_ENV = {
    "github": ("GITHUB_TOKEN", "GH_TOKEN"),
    "gitea": ("GITEA_TOKEN",),
    "forgejo": ("FORGEJO_TOKEN", "GITEA_TOKEN"),
    "gitlab": ("GITLAB_TOKEN",),
}

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_CATEGORY_SLOT = re.compile(r"^category([1-9]|1[0-9]|2[0-5])$")


# --------------------------------------------------------------------------- dataclasses


@dataclass
class GraphConfig:
    tenant_id: str | None = None
    client_id: str | None = None
    auth: str = "auto"
    client_secret: str | None = None
    client_assertion_file: str | None = None
    access_token: str | None = None
    token_cache: str = DEFAULT_TOKEN_CACHE
    authority: str = "https://login.microsoftonline.com"
    graph_url: str = "https://graph.microsoft.com/v1.0"
    scopes: list[str] = field(default_factory=lambda: list(DEFAULT_DELEGATED_SCOPES))
    timeout: float = 30.0

    def resolved_auth(self) -> str:
        """Return the concrete authentication method, inferring it when set to ``auto``."""
        if self.auth != "auto":
            return self.auth
        if self.access_token:
            return "access_token"
        if self.client_secret:
            return "client_secret"
        if self.client_assertion_file:
            return "federated"
        return "device_code"


@dataclass
class SourceConfig:
    provider: str
    repository: str
    url: str | None = None
    token: str | None = None
    verify_ssl: bool | str = True
    timeout: float = 30.0

    @property
    def owner(self) -> str:
        return self.repository.rsplit("/", 1)[0]

    @property
    def repo(self) -> str:
        return self.repository.rsplit("/", 1)[1]


@dataclass
class FilterConfig:
    state: str = "open"
    labels: list[str] = field(default_factory=list)
    exclude_labels: list[str] = field(default_factory=list)
    milestones: list[str] = field(default_factory=list)
    created_after: datetime | None = None
    include_pull_requests: bool = False


@dataclass
class BoardConfig:
    """A project board whose columns are mirrored as Planner buckets."""

    project: str | None = None
    """GitHub project number, Gitea project id or title, GitLab board id or name."""
    owner: str | None = None
    """User or organisation owning the project (GitHub, Gitea); defaults to the repository owner."""
    field: str = "Status"
    """GitHub Projects single-select field holding the column."""


@dataclass
class BucketConfig:
    strategy: str = "fixed"
    default: str = "To do"
    mapping: dict[str, str] = field(default_factory=dict)
    closed: str | None = None
    create_missing: bool = True
    board: BoardConfig = field(default_factory=BoardConfig)


@dataclass
class CategoryConfig:
    mode: str = "auto"
    mapping: dict[str, str] = field(default_factory=dict)
    exclude: list[str] = field(default_factory=list)
    create_missing: bool = True


@dataclass
class AssigneeConfig:
    enabled: bool = True
    mapping: dict[str, str] = field(default_factory=dict)
    use_git_email: bool = True
    email_domain: str | None = None
    require_group_membership: bool = True
    fallback: list[str] = field(default_factory=list)


@dataclass
class PriorityConfig:
    mapping: dict[str, int] = field(default_factory=dict)
    default: int = 5


@dataclass
class AttachmentConfig:
    mode: str = "link"
    folder: str = "git2planner/{owner}/{repo}/{number}"
    max_size_mb: float = 25.0
    link_issue: bool = True


@dataclass
class TaskConfig:
    title_template: str = "#{number} {title}"
    description: bool = True
    footer: bool = True
    checklist: bool = True
    due_date: bool = True
    preview: str = "automatic"


@dataclass
class PlannerToGitConfig:
    fields: list[str] = field(default_factory=lambda: list(DEFAULT_PULL_FIELDS))
    create_issues: bool = False
    buckets: list[str] = field(default_factory=list)
    include_completed: bool = False
    close_deleted: bool = False


@dataclass
class SyncConfig:
    name: str
    source: SourceConfig
    plan_id: str
    filters: FilterConfig = field(default_factory=FilterConfig)
    buckets: BucketConfig = field(default_factory=BucketConfig)
    categories: CategoryConfig = field(default_factory=CategoryConfig)
    assignees: AssigneeConfig = field(default_factory=AssigneeConfig)
    priority: PriorityConfig = field(default_factory=PriorityConfig)
    attachments: AttachmentConfig = field(default_factory=AttachmentConfig)
    task: TaskConfig = field(default_factory=TaskConfig)
    on_close: str = "complete"
    on_missing: str = "keep"
    recreate_deleted: bool = False
    direction: str = "git-to-planner"
    conflicts: str = "git"
    planner_to_git: PlannerToGitConfig = field(default_factory=PlannerToGitConfig)

    @property
    def pushes(self) -> bool:
        """Whether issue changes are written to Planner."""
        return self.direction in ("git-to-planner", "both")

    @property
    def pulls(self) -> bool:
        """Whether Planner changes are written back to the Git provider."""
        return self.direction in ("planner-to-git", "both")


def normalize_direction(value: str) -> str:
    text = value.strip().lower().replace("_", "-")
    text = DIRECTION_ALIASES.get(text, text)
    if text not in DIRECTIONS:
        raise ConfigError(f"invalid direction {value!r} (expected one of: {', '.join(DIRECTIONS)})")
    return text


@dataclass
class AppConfig:
    graph: GraphConfig
    syncs: list[SyncConfig]
    state_path: Path | None = None
    path: Path | None = None

    def select(self, names: list[str] | None) -> list[SyncConfig]:
        if not names:
            return list(self.syncs)
        known = {sync.name: sync for sync in self.syncs}
        missing = [name for name in names if name not in known]
        if missing:
            raise ConfigError(
                f"unknown sync name(s): {', '.join(missing)} "
                f"(configured: {', '.join(known) or 'none'})"
            )
        return [known[name] for name in names]


# --------------------------------------------------------------------------- helpers


def interpolate_env(value: Any, environ: Mapping[str, str], path: str = "") -> Any:
    """Recursively replace ``${VAR}`` and ``${VAR:-default}`` in strings."""
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in environ and environ[name] != "":
                return environ[name]
            if default is not None:
                return default
            raise ConfigError(
                f"environment variable {name} is not set (referenced in {path or 'config'})"
            )

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [
            interpolate_env(item, environ, f"{path}[{index}]") for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        return {
            key: interpolate_env(item, environ, f"{path}.{key}" if path else str(key))
            for key, item in value.items()
        }
    return value


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Merge ``override`` into a copy of ``base``; nested mappings are merged, lists replaced."""
    result: dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def template_fields(template: str) -> set[str]:
    try:
        return {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}
    except ValueError as exc:
        raise ConfigError(f"invalid template {template!r}: {exc}") from exc


def parse_priority(value: Any, path: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{path}: invalid priority {value!r}")
    if isinstance(value, int):
        if 0 <= value <= 10:
            return value
        raise ConfigError(f"{path}: priority must be between 0 and 10, got {value}")
    if isinstance(value, str):
        text = value.strip().lower()
        if text in PRIORITY_NAMES:
            return PRIORITY_NAMES[text]
        if text.isdigit():
            return parse_priority(int(text), path)
    raise ConfigError(
        f"{path}: invalid priority {value!r} (use urgent, important, medium, low or 0-10)"
    )


def is_category_slot(value: str) -> bool:
    return bool(_CATEGORY_SLOT.match(value))


class _Section:
    """Typed accessors over a mapping with error messages that carry the key path."""

    def __init__(self, data: Any, path: str, allowed: set[str]) -> None:
        if data is None:
            data = {}
        if not isinstance(data, Mapping):
            raise ConfigError(f"{path}: expected a mapping, got {type(data).__name__}")
        unknown = sorted(set(map(str, data)) - allowed)
        if unknown:
            raise ConfigError(
                f"{path}: unknown key(s) {', '.join(unknown)} "
                f"(allowed: {', '.join(sorted(allowed))})"
            )
        self.data = dict(data)
        self.path = path

    def _key(self, key: str) -> str:
        return f"{self.path}.{key}" if self.path else key

    def raw(self, key: str) -> Any:
        return self.data.get(key)

    def text(
        self,
        key: str,
        default: str | None = None,
        *,
        required: bool = False,
        choices: tuple[str, ...] | None = None,
    ) -> str | None:
        value = self.data.get(key)
        if value is None or value == "":
            if required:
                raise ConfigError(f"{self._key(key)} is required")
            return default
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ConfigError(f"{self._key(key)}: expected a string, got {type(value).__name__}")
        text = str(value).strip()
        if choices is not None and text not in choices:
            raise ConfigError(
                f"{self._key(key)}: invalid value {text!r} (expected one of: {', '.join(choices)})"
            )
        return text

    def req(self, key: str, *, choices: tuple[str, ...] | None = None) -> str:
        value = self.text(key, required=True, choices=choices)
        assert value is not None
        return value

    def flag(self, key: str, default: bool) -> bool:
        value = self.data.get(key)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "yes", "on", "1"):
            return True
        if isinstance(value, str) and value.strip().lower() in ("false", "no", "off", "0"):
            return False
        raise ConfigError(f"{self._key(key)}: expected true or false, got {value!r}")

    def number(self, key: str, default: float, *, minimum: float = 0) -> float:
        value = self.data.get(key)
        if value is None:
            return default
        if isinstance(value, bool):
            raise ConfigError(f"{self._key(key)}: expected a number, got {value!r}")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{self._key(key)}: expected a number, got {value!r}") from exc
        if number < minimum:
            raise ConfigError(f"{self._key(key)}: must be >= {minimum}")
        return number

    def str_list(self, key: str) -> list[str]:
        value = self.data.get(key)
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if not isinstance(value, list) or not all(isinstance(item, (str, int)) for item in value):
            raise ConfigError(f"{self._key(key)}: expected a list of strings")
        return [str(item) for item in value]

    def str_map(self, key: str) -> dict[str, Any]:
        value = self.data.get(key)
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ConfigError(f"{self._key(key)}: expected a mapping")
        result: dict[str, Any] = {}
        for item_key, item_value in value.items():
            if item_value is None:
                raise ConfigError(f"{self._key(key)}.{item_key}: value must not be empty")
            result[str(item_key)] = item_value
        return result

    def section(self, key: str, allowed: set[str]) -> _Section:
        return _Section(self.data.get(key), self._key(key), allowed)


# --------------------------------------------------------------------------- parsing


def _parse_graph(section: _Section, environ: Mapping[str, str]) -> GraphConfig:
    cfg = GraphConfig(
        tenant_id=section.text("tenant_id") or environ.get("AZURE_TENANT_ID") or None,
        client_id=section.text("client_id") or environ.get("AZURE_CLIENT_ID") or None,
        auth=section.text("auth", "auto", choices=AUTH_METHODS) or "auto",
        client_secret=section.text("client_secret") or environ.get("AZURE_CLIENT_SECRET") or None,
        client_assertion_file=section.text("client_assertion_file")
        or environ.get("AZURE_FEDERATED_TOKEN_FILE")
        or None,
        access_token=section.text("access_token") or environ.get("GRAPH_ACCESS_TOKEN") or None,
        token_cache=section.text("token_cache", DEFAULT_TOKEN_CACHE) or DEFAULT_TOKEN_CACHE,
        authority=(section.text("authority") or "https://login.microsoftonline.com").rstrip("/"),
        graph_url=(section.text("graph_url") or "https://graph.microsoft.com/v1.0").rstrip("/"),
        timeout=section.number("timeout", 30.0, minimum=1),
    )
    scopes = section.str_list("scopes")
    if scopes:
        cfg.scopes = scopes

    method = cfg.resolved_auth()
    needs = {
        "client_secret": ("tenant_id", "client_id", "client_secret"),
        "federated": ("tenant_id", "client_id", "client_assertion_file"),
        "github_oidc": ("tenant_id", "client_id"),
        "device_code": ("client_id",),
        "access_token": ("access_token",),
    }[method]
    env_names = {
        "tenant_id": "AZURE_TENANT_ID",
        "client_id": "AZURE_CLIENT_ID",
        "client_secret": "AZURE_CLIENT_SECRET",
        "client_assertion_file": "AZURE_FEDERATED_TOKEN_FILE",
        "access_token": "GRAPH_ACCESS_TOKEN",
    }
    missing = [name for name in needs if not getattr(cfg, name)]
    if missing:
        details = ", ".join(f"graph.{name} (or ${env_names[name]})" for name in missing)
        raise ConfigError(f"authentication method {method!r} requires {details}")
    if method == "device_code" and not cfg.tenant_id:
        cfg.tenant_id = "organizations"
    return cfg


def _parse_source(section: _Section, environ: Mapping[str, str]) -> SourceConfig:
    provider = section.req("provider", choices=PROVIDERS)
    repository = section.req("repository").strip("/")
    if repository.count("/") < 1 or (provider != "gitlab" and repository.count("/") != 1):
        expected = "group/[subgroup/]project" if provider == "gitlab" else "owner/name"
        raise ConfigError(f"{section.path}.repository: expected {expected!r}, got {repository!r}")
    url = section.text("url")
    if provider in ("gitea", "forgejo") and not url:
        raise ConfigError(f"{section.path}.url is required for {provider}")
    if url and not url.startswith(("http://", "https://")):
        raise ConfigError(f"{section.path}.url must start with http:// or https://")
    token = section.text("token")
    if not token:
        for name in TOKEN_ENV[provider]:
            if environ.get(name):
                token = environ[name]
                break
    verify_raw = section.raw("verify_ssl")
    verify: bool | str
    if verify_raw is None or isinstance(verify_raw, bool):
        verify = True if verify_raw is None else verify_raw
    elif isinstance(verify_raw, str):
        lowered = verify_raw.strip().lower()
        if lowered in ("true", "false"):
            verify = lowered == "true"
        else:
            verify = os.path.expanduser(verify_raw)
    else:
        raise ConfigError(f"{section.path}.verify_ssl: expected true, false or a CA bundle path")
    return SourceConfig(
        provider=provider,
        repository=repository,
        url=url.rstrip("/") if url else None,
        token=token or None,
        verify_ssl=verify,
        timeout=section.number("timeout", 30.0, minimum=1),
    )


def _parse_date(value: Any, path: str) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ConfigError(f"{path}: invalid date {value!r} (use YYYY-MM-DD)") from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    raise ConfigError(f"{path}: invalid date {value!r}")


def _parse_sync(raw: Any, index: int, environ: Mapping[str, str]) -> SyncConfig:
    path = f"syncs[{index}]"
    section = _Section(
        raw,
        path,
        {
            "name",
            "source",
            "plan_id",
            "filters",
            "buckets",
            "categories",
            "assignees",
            "priority",
            "attachments",
            "task",
            "on_close",
            "on_missing",
            "recreate_deleted",
            "direction",
            "conflicts",
            "planner_to_git",
        },
    )
    source = _parse_source(
        section.section(
            "source", {"provider", "repository", "url", "token", "verify_ssl", "timeout"}
        ),
        environ,
    )
    plan_id = section.req("plan_id")
    name = section.text("name") or source.repository

    filters_s = section.section(
        "filters",
        {
            "state",
            "labels",
            "exclude_labels",
            "milestones",
            "created_after",
            "include_pull_requests",
        },
    )
    filters = FilterConfig(
        state=filters_s.text("state", "open", choices=ISSUE_STATES) or "open",
        labels=filters_s.str_list("labels"),
        exclude_labels=filters_s.str_list("exclude_labels"),
        milestones=filters_s.str_list("milestones"),
        created_after=_parse_date(
            filters_s.raw("created_after"), f"{filters_s.path}.created_after"
        ),
        include_pull_requests=filters_s.flag("include_pull_requests", False),
    )

    buckets_s = section.section(
        "buckets", {"strategy", "default", "mapping", "closed", "create_missing", "board"}
    )
    board_s = buckets_s.section("board", {"project", "owner", "field"})
    buckets = BucketConfig(
        strategy=buckets_s.text("strategy", "fixed", choices=BUCKET_STRATEGIES) or "fixed",
        default=buckets_s.text("default", "To do") or "To do",
        mapping={key: str(value) for key, value in buckets_s.str_map("mapping").items()},
        closed=buckets_s.text("closed"),
        create_missing=buckets_s.flag("create_missing", True),
        board=BoardConfig(
            project=board_s.text("project"),
            owner=board_s.text("owner"),
            field=board_s.text("field", "Status") or "Status",
        ),
    )
    if buckets.strategy == "board":
        if source.provider == "forgejo":
            raise ConfigError(
                f"{buckets_s.path}.strategy: Forgejo has no project board API yet, "
                "use the 'label' strategy instead"
            )
        if source.provider in ("github", "gitea") and not buckets.board.project:
            raise ConfigError(f"{board_s.path}.project is required for {source.provider}")
        if source.provider == "github" and not str(buckets.board.project).isdigit():
            raise ConfigError(
                f"{board_s.path}.project: use the project number shown in its URL "
                "(.../projects/<number>)"
            )

    categories_s = section.section("categories", {"mode", "mapping", "exclude", "create_missing"})
    categories = CategoryConfig(
        mode=categories_s.text("mode", "auto", choices=CATEGORY_MODES) or "auto",
        mapping={key: str(value).strip() for key, value in categories_s.str_map("mapping").items()},
        exclude=categories_s.str_list("exclude"),
        create_missing=categories_s.flag("create_missing", True),
    )

    assignees_s = section.section(
        "assignees",
        {
            "enabled",
            "mapping",
            "use_git_email",
            "email_domain",
            "require_group_membership",
            "fallback",
        },
    )
    email_domain = assignees_s.text("email_domain")
    assignees = AssigneeConfig(
        enabled=assignees_s.flag("enabled", True),
        mapping={key: str(value).strip() for key, value in assignees_s.str_map("mapping").items()},
        use_git_email=assignees_s.flag("use_git_email", True),
        email_domain=email_domain.lstrip("@") if email_domain else None,
        require_group_membership=assignees_s.flag("require_group_membership", True),
        fallback=assignees_s.str_list("fallback"),
    )

    priority_s = section.section("priority", {"mapping", "default"})
    priority = PriorityConfig(
        mapping={
            key: parse_priority(value, f"{priority_s.path}.mapping.{key}")
            for key, value in priority_s.str_map("mapping").items()
        },
        default=parse_priority(
            priority_s.raw("default") if priority_s.raw("default") is not None else "medium",
            f"{priority_s.path}.default",
        ),
    )

    attachments_s = section.section("attachments", {"mode", "folder", "max_size_mb", "link_issue"})
    attachments = AttachmentConfig(
        mode=attachments_s.text("mode", "link", choices=ATTACHMENT_MODES) or "link",
        folder=attachments_s.text("folder", AttachmentConfig.folder) or AttachmentConfig.folder,
        max_size_mb=attachments_s.number("max_size_mb", 25.0, minimum=0.001),
        link_issue=attachments_s.flag("link_issue", True),
    )
    unknown = template_fields(attachments.folder) - set(FOLDER_FIELDS)
    if unknown:
        raise ConfigError(
            f"{attachments_s.path}.folder: unknown placeholder(s) {', '.join(sorted(unknown))} "
            f"(available: {', '.join(FOLDER_FIELDS)})"
        )

    task_s = section.section(
        "task", {"title_template", "description", "footer", "checklist", "due_date", "preview"}
    )
    task = TaskConfig(
        title_template=task_s.text("title_template", TaskConfig.title_template)
        or TaskConfig.title_template,
        description=task_s.flag("description", True),
        footer=task_s.flag("footer", True),
        checklist=task_s.flag("checklist", True),
        due_date=task_s.flag("due_date", True),
        preview=task_s.text("preview", "automatic", choices=PREVIEW_TYPES) or "automatic",
    )
    unknown = template_fields(task.title_template) - set(TITLE_FIELDS)
    if unknown:
        raise ConfigError(
            f"{task_s.path}.title_template: unknown placeholder(s) {', '.join(sorted(unknown))} "
            f"(available: {', '.join(TITLE_FIELDS)})"
        )

    pull_s = section.section(
        "planner_to_git",
        {"fields", "create_issues", "buckets", "include_completed", "close_deleted"},
    )
    pull_fields = (
        pull_s.str_list("fields") if pull_s.raw("fields") is not None else list(DEFAULT_PULL_FIELDS)
    )
    unknown_fields = sorted(set(pull_fields) - set(PULL_FIELDS))
    if unknown_fields:
        raise ConfigError(
            f"{pull_s.path}.fields: unknown field(s) {', '.join(unknown_fields)} "
            f"(available: {', '.join(PULL_FIELDS)})"
        )
    planner_to_git = PlannerToGitConfig(
        fields=pull_fields,
        create_issues=pull_s.flag("create_issues", False),
        buckets=pull_s.str_list("buckets"),
        include_completed=pull_s.flag("include_completed", False),
        close_deleted=pull_s.flag("close_deleted", False),
    )
    direction_raw = section.text("direction", "git-to-planner") or "git-to-planner"
    try:
        direction = normalize_direction(direction_raw)
    except ConfigError as exc:
        raise ConfigError(f"{path}.direction: {exc}") from exc

    return SyncConfig(
        name=name,
        source=source,
        plan_id=plan_id,
        filters=filters,
        buckets=buckets,
        categories=categories,
        assignees=assignees,
        priority=priority,
        attachments=attachments,
        task=task,
        on_close=section.text("on_close", "complete", choices=ON_CLOSE) or "complete",
        on_missing=section.text("on_missing", "keep", choices=ON_MISSING) or "keep",
        recreate_deleted=section.flag("recreate_deleted", False),
        direction=direction,
        conflicts=section.text("conflicts", "git", choices=CONFLICT_POLICIES) or "git",
        planner_to_git=planner_to_git,
    )


def parse_config(
    data: Any,
    *,
    environ: Mapping[str, str] | None = None,
    base_dir: Path | None = None,
) -> AppConfig:
    """Build an :class:`AppConfig` from already-parsed YAML data."""
    env = os.environ if environ is None else environ
    if data is None:
        raise ConfigError("configuration is empty")
    root = _Section(data, "", {"graph", "state", "defaults", "syncs"})
    resolved = interpolate_env(root.data, env)
    root = _Section(resolved, "", {"graph", "state", "defaults", "syncs"})

    graph = _parse_graph(
        root.section(
            "graph",
            {
                "tenant_id",
                "client_id",
                "auth",
                "client_secret",
                "client_assertion_file",
                "access_token",
                "token_cache",
                "authority",
                "graph_url",
                "scopes",
                "timeout",
            },
        ),
        env,
    )

    state_s = root.section("state", {"path", "enabled"})
    state_path: Path | None = None
    if state_s.flag("enabled", True):
        state_path = Path(os.path.expanduser(state_s.text("path", DEFAULT_STATE_FILE) or ""))
        if base_dir is not None and not state_path.is_absolute():
            state_path = base_dir / state_path

    defaults = root.raw("defaults") or {}
    if not isinstance(defaults, Mapping):
        raise ConfigError("defaults: expected a mapping")
    raw_syncs = root.raw("syncs")
    if not raw_syncs:
        raise ConfigError("syncs: at least one sync must be configured")
    if not isinstance(raw_syncs, list):
        raise ConfigError("syncs: expected a list")

    syncs: list[SyncConfig] = []
    for index, raw in enumerate(raw_syncs):
        if not isinstance(raw, Mapping):
            raise ConfigError(f"syncs[{index}]: expected a mapping")
        syncs.append(_parse_sync(deep_merge(defaults, raw), index, env))

    names = [sync.name for sync in syncs]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ConfigError(
            f"syncs: duplicate name(s) {', '.join(duplicates)}; give each sync a unique 'name'"
        )
    return AppConfig(graph=graph, syncs=syncs, state_path=state_path)


def load_config(
    path: str | os.PathLike[str], *, environ: Mapping[str, str] | None = None
) -> AppConfig:
    """Read and validate a configuration file."""
    config_path = Path(path).expanduser()
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(
            f"configuration file {config_path} not found (create one with 'git2planner init')"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {config_path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path}: invalid YAML: {exc}") from exc
    config = parse_config(data, environ=environ, base_dir=config_path.resolve().parent)
    config.path = config_path
    return config
