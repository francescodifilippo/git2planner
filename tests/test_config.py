from __future__ import annotations

from datetime import datetime, timezone

import pytest
import yaml

from git2planner.config import (
    DEFAULT_PULL_FIELDS,
    interpolate_env,
    load_config,
    parse_config,
    parse_priority,
)
from git2planner.errors import ConfigError
from git2planner.templates import example_config


def minimal(**sync):
    data = {"provider": "github", "repository": "acme/app"}
    return {
        "graph": {"tenant_id": "t", "client_id": "c", "client_secret": "s"},
        "syncs": [{"source": data, "plan_id": "p", **sync}],
    }


def test_minimal_config_defaults():
    config = parse_config(minimal(), environ={})
    sync = config.syncs[0]
    assert sync.name == "acme/app"
    assert sync.direction == "git-to-planner"
    assert sync.filters.state == "open"
    assert sync.categories.mode == "auto"
    assert sync.priority.default == 5
    assert sync.planner_to_git.fields == list(DEFAULT_PULL_FIELDS)
    assert config.graph.resolved_auth() == "client_secret"
    assert config.state_path.as_posix().endswith(".git2planner/state.json")


def test_environment_interpolation_and_defaults():
    data = minimal()
    data["syncs"][0]["source"]["token"] = "${TOKEN}"
    data["syncs"][0]["plan_id"] = "${PLAN:-fallback}"
    config = parse_config(data, environ={"TOKEN": "abc"})
    assert config.syncs[0].source.token == "abc"
    assert config.syncs[0].plan_id == "fallback"


def test_missing_environment_variable_is_reported():
    with pytest.raises(ConfigError, match="MISSING"):
        interpolate_env({"a": ["${MISSING}"]}, {})


def test_token_and_graph_settings_from_environment():
    data = {"syncs": [{"source": {"provider": "gitlab", "repository": "g/s/p"}, "plan_id": "p"}]}
    config = parse_config(
        data,
        environ={
            "AZURE_TENANT_ID": "t",
            "AZURE_CLIENT_ID": "c",
            "AZURE_CLIENT_SECRET": "s",
            "GITLAB_TOKEN": "g",
        },
    )
    assert config.syncs[0].source.token == "g"
    assert config.syncs[0].source.owner == "g/s"
    assert config.syncs[0].source.repo == "p"
    assert config.graph.client_secret == "s"


def test_device_code_defaults_to_organizations_tenant():
    data = {"graph": {"client_id": "c"}, "syncs": minimal()["syncs"]}
    config = parse_config(data, environ={})
    assert config.graph.resolved_auth() == "device_code"
    assert config.graph.tenant_id == "organizations"


def test_auth_requirements():
    data = {"graph": {"auth": "client_secret", "client_id": "c"}, "syncs": minimal()["syncs"]}
    with pytest.raises(ConfigError, match="client_secret"):
        parse_config(data, environ={})


def test_defaults_are_merged_into_syncs():
    data = minimal()
    data["defaults"] = {"buckets": {"default": "Backlog", "closed": "Done"}, "direction": "both"}
    data["syncs"][0]["buckets"] = {"closed": "Archive"}
    sync = parse_config(data, environ={}).syncs[0]
    assert sync.buckets.default == "Backlog"
    assert sync.buckets.closed == "Archive"
    assert sync.direction == "both"


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"unknown": 1}, "unknown key"),
        ({"filters": {"state": "maybe"}}, "invalid value"),
        ({"task": {"title_template": "{nope}"}}, "unknown placeholder"),
        ({"attachments": {"folder": "{title}"}}, "unknown placeholder"),
        ({"priority": {"default": "whenever"}}, "invalid priority"),
        ({"direction": "sideways"}, "invalid direction"),
        ({"planner_to_git": {"fields": ["colour"]}}, "unknown field"),
        ({"filters": {"created_after": "yesterday"}}, "invalid date"),
    ],
)
def test_invalid_values(patch, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(minimal(**patch), environ={})


def test_repository_shapes():
    data = minimal()
    data["syncs"][0]["source"] = {"provider": "github", "repository": "a/b/c"}
    with pytest.raises(ConfigError, match="owner/name"):
        parse_config(data, environ={})
    data["syncs"][0]["source"] = {"provider": "gitea", "repository": "a/b"}
    with pytest.raises(ConfigError, match="url is required"):
        parse_config(data, environ={})


def test_duplicate_sync_names():
    data = minimal()
    data["syncs"].append(dict(data["syncs"][0]))
    with pytest.raises(ConfigError, match="duplicate"):
        parse_config(data, environ={})


def test_direction_aliases_and_dates():
    config = parse_config(
        minimal(direction="bidirectional", filters={"created_after": "2025-02-03"}), environ={}
    )
    sync = config.syncs[0]
    assert sync.direction == "both"
    assert sync.pushes
    assert sync.pulls
    assert sync.filters.created_after == datetime(2025, 2, 3, tzinfo=timezone.utc)


def test_priority_parsing():
    assert parse_priority("Urgent", "x") == 1
    assert parse_priority(7, "x") == 7
    assert parse_priority("9", "x") == 9
    with pytest.raises(ConfigError):
        parse_priority(11, "x")
    with pytest.raises(ConfigError):
        parse_priority(True, "x")


def test_select_unknown_name():
    config = parse_config(minimal(), environ={})
    with pytest.raises(ConfigError, match="unknown sync"):
        config.select(["nope"])


@pytest.mark.parametrize("provider", ["github", "gitea", "forgejo", "gitlab"])
def test_example_config_is_valid(provider, tmp_path):
    path = tmp_path / "git2planner.yml"
    path.write_text(example_config(provider), encoding="utf-8")
    config = load_config(path, environ={"AZURE_CLIENT_ID": "c"})
    assert config.syncs[0].source.provider == provider
    assert config.state_path == tmp_path / ".git2planner/state.json"
    assert yaml.safe_load(example_config(provider))["syncs"][0]["name"] == "my-project"


def test_load_config_errors(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yml")
    bad = tmp_path / "bad.yml"
    bad.write_text("syncs: [", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(bad)
