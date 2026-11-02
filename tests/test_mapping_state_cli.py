from __future__ import annotations

import json
import logging

import pytest

from git2planner import __version__, cli
from git2planner.config import AssigneeConfig, BucketConfig, CategoryConfig, PriorityConfig
from git2planner.errors import PermissionDeniedError, StateError
from git2planner.mapping import (
    AssigneeResolver,
    CategoryMapper,
    bucket_label,
    bucket_name_for,
    color_distance,
    priority_for,
    priority_label,
)
from git2planner.models import GitUser, Label, Milestone
from git2planner.state import StateStore, TaskRecord

from .fakes import FakePlanner, FakeProvider

# --------------------------------------------------------------------------- categories


def test_auto_mode_reuses_names_and_picks_closest_colour():
    mapper = CategoryMapper(CategoryConfig(), {"category1": "Bug", "category2": None})
    assert mapper.resolve([Label("bug")]) == ["category1"]
    assert mapper.resolve([Label("enhancement", "0078d4")]) == ["category5"]
    assert mapper.pending == {"category5": "enhancement"}
    assert mapper.resolve([Label("docs")]) == ["category2"]


def test_explicit_mode_with_slots_names_and_excludes():
    config = CategoryConfig(
        mode="explicit",
        mapping={"security": "category13", "ui": "Frontend", "wip": "Work"},
        exclude=["wip"],
        create_missing=False,
    )
    mapper = CategoryMapper(config, {"category4": "frontend"})
    labels = [Label("security"), Label("UI"), Label("random"), Label("wip")]
    assert mapper.resolve(labels) == ["category4", "category13"]
    assert mapper.label_for("category13") == "security"
    assert mapper.label_for("category4") == "ui"
    assert mapper.label_for("category9") is None


def test_category_slots_exhausted(caplog):
    mapper = CategoryMapper(CategoryConfig(), {f"category{i}": f"n{i}" for i in range(1, 26)})
    with caplog.at_level(logging.WARNING):
        assert mapper.resolve([Label("new")]) == []
    assert "no free Planner category" in caplog.text


def test_off_mode_and_auto_reverse_mapping():
    assert CategoryMapper(CategoryConfig(mode="off"), {}).resolve([Label("bug")]) == []
    mapper = CategoryMapper(
        CategoryConfig(exclude=["internal"]), {"category3": "bug", "category4": "internal"}
    )
    assert mapper.label_for("category3") == "bug"
    assert mapper.label_for("category4") is None


def test_colour_distance_orders_sensibly():
    assert color_distance("ff0000", "d13438") < color_distance("ff0000", "0078d4")


# --------------------------------------------------------------------------- buckets & priority


def test_bucket_strategies():
    provider = FakeProvider()
    issue = provider.add(1, "x", labels=["backend"], milestone=Milestone("Sprint 3"))
    assert bucket_name_for(issue, BucketConfig()) == "To do"
    assert (
        bucket_name_for(issue, BucketConfig(strategy="label", mapping={"Backend": "Server"}))
        == "Server"
    )
    assert bucket_name_for(issue, BucketConfig(strategy="milestone")) == "Sprint 3"
    assert (
        bucket_name_for(issue, BucketConfig(strategy="milestone", mapping={"sprint 3": "Now"}))
        == "Now"
    )
    issue.state = "closed"
    assert bucket_name_for(issue, BucketConfig(closed="Done")) == "Done"
    assert (
        bucket_label(BucketConfig(strategy="label", mapping={"backend": "Server"}), "server")
        == "backend"
    )
    assert bucket_label(BucketConfig(), "server") is None


def test_priority_mapping_both_ways():
    config = PriorityConfig(mapping={"p0": 1, "p1": 3, "p3": 9}, default=5)
    provider = FakeProvider()
    assert priority_for(provider.add(1, "x", labels=["p3", "p0"]), config) == 1
    assert priority_for(provider.add(2, "x"), config) == 5
    assert priority_label(config, 0) == "p0"
    assert priority_label(config, 2) == "p1"
    assert priority_label(config, 6) is None


# --------------------------------------------------------------------------- assignees


def resolver(planner: FakePlanner, provider: FakeProvider, **config) -> AssigneeResolver:
    return AssigneeResolver(AssigneeConfig(**config), planner, provider, "group-1")  # type: ignore[arg-type]


def test_assignee_resolution_order_and_reverse():
    planner, provider = FakePlanner(), FakeProvider()
    planner.add_user("u1", "alice@contoso.com")
    planner.add_user("u2", "bob@contoso.com", mail="robert@contoso.com")
    planner.add_user("u3", "carol@contoso.com")
    provider.profiles["bobby"] = GitUser("bobby", email="robert@contoso.com")
    res = resolver(
        planner, provider, mapping={"ally": "alice@contoso.com"}, email_domain="contoso.com"
    )
    users = [GitUser("ally"), GitUser("bobby"), GitUser("carol"), GitUser("ghost")]
    assert res.resolve(users) == ["u1", "u2", "u3"]
    assert res.login_for("u1") == "ally"
    assert res.login_for("u3") == "carol"

    fresh = resolver(
        planner, provider, mapping={"ally": "alice@contoso.com"}, email_domain="contoso.com"
    )
    assert fresh.login_for("u1") == "ally"
    assert fresh.login_for("u3") == "carol"
    assert fresh.login_for("unknown") is None


def test_assignee_group_membership_and_fallback():
    planner, provider = FakePlanner(), FakeProvider()
    planner.add_user("u1", "alice@contoso.com", member=False)
    planner.add_user("u9", "lead@contoso.com")
    res = resolver(
        planner, provider, mapping={"alice": "alice@contoso.com"}, fallback=["lead@contoso.com"]
    )
    assert res.resolve([GitUser("alice")]) == ["u9"]
    relaxed = resolver(
        planner, provider, mapping={"alice": "alice@contoso.com"}, require_group_membership=False
    )
    assert relaxed.resolve([GitUser("alice")]) == ["u1"]
    assert resolver(planner, provider, enabled=False).resolve([GitUser("alice")]) == []


def test_object_ids_and_noreply_addresses():
    planner, provider = FakePlanner(), FakeProvider()
    planner.members.add("00000000-0000-0000-0000-000000000001")
    provider.profiles["x"] = GitUser("x", email="x@users.noreply.github.com")
    res = resolver(planner, provider, mapping={"y": "00000000-0000-0000-0000-000000000001"})
    assert res.resolve([GitUser("x"), GitUser("y")]) == ["00000000-0000-0000-0000-000000000001"]


def test_directory_permission_errors_disable_lookups(caplog):
    planner, provider = FakePlanner(), FakeProvider()

    def denied(identifier):
        raise PermissionDeniedError("forbidden", status=403)

    planner.get_user = denied  # type: ignore[method-assign]
    res = resolver(planner, provider, mapping={"a": "a@contoso.com", "b": "b@contoso.com"})
    with caplog.at_level(logging.ERROR):
        assert res.resolve([GitUser("a"), GitUser("b")]) == []
    assert caplog.text.count("User.Read.All") == 1


# --------------------------------------------------------------------------- state


def test_state_roundtrip_and_lock(tmp_path):
    path = tmp_path / "nested" / "state.json"
    store = StateStore(path)
    sync = store.sync("app")
    sync.plan_id = "p"
    sync.issues["2"] = TaskRecord(
        task_id="t2", issue_url="u2", percent_complete=0, checklist={"a": False}
    )
    sync.issues["10"] = TaskRecord(task_id="t10", issue_url="u10", origin="planner")
    store.save()
    data = json.loads(path.read_text())
    assert list(data["syncs"]["app"]["issues"]) == ["2", "10"]
    assert data["syncs"]["app"]["issues"]["2"]["percent_complete"] == 0
    assert "missing" not in data["syncs"]["app"]["issues"]["2"]

    loaded = StateStore(path).load()
    assert loaded.sync("app").issues["10"].origin == "planner"
    assert loaded.sync("app").issues["2"].checklist == {"a": False}

    with (
        store.locked(),
        pytest.raises(StateError, match="another git2planner run"),
        StateStore(path).locked(),
    ):
        pass


def test_state_rejects_newer_format(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 99, "syncs": {}}))
    with pytest.raises(StateError, match="newer"):
        StateStore(path).load()
    path.write_text("{broken")
    with pytest.raises(StateError, match="cannot read"):
        StateStore(path).load()


# --------------------------------------------------------------------------- cli


def test_cli_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])
    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_cli_init_and_offline_validate(tmp_path, capsys, monkeypatch):
    target = tmp_path / "git2planner.yml"
    assert cli.main(["init", str(target), "--provider", "gitlab"]) == 0
    assert "provider: gitlab" in target.read_text()
    assert cli.main(["init", str(target)]) == cli.EXIT_CONFIG
    monkeypatch.setenv("AZURE_CLIENT_ID", "client")
    assert cli.main(["-c", str(target), "validate", "--offline"]) == 0
    assert "is valid" in capsys.readouterr().out


def test_cli_reports_config_errors(tmp_path, capsys):
    assert cli.main(["-c", str(tmp_path / "missing.yml"), "sync"]) == cli.EXIT_CONFIG
    assert "configuration error" in capsys.readouterr().err
    assert cli.main([]) == cli.EXIT_CONFIG


def test_cli_sync_end_to_end(tmp_path, capsys, monkeypatch):
    config = tmp_path / "git2planner.yml"
    config.write_text(
        """
graph: {access_token: token}
state: {path: state.json}
syncs:
  - name: app
    source: {provider: github, repository: acme/app, token: t}
    plan_id: plan-1
""",
        encoding="utf-8",
    )
    planner, provider = FakePlanner(), FakeProvider()
    provider.add(1, "From CLI")
    monkeypatch.setattr(cli.GraphClient, "from_config", classmethod(lambda cls, *a, **k: planner))
    monkeypatch.setattr("git2planner.sync.create_provider", lambda _: provider)

    assert cli.main(["-q", "-c", str(config), "sync", "--json"]) == 0
    (report,) = json.loads(capsys.readouterr().out)
    assert report["created"] == 1
    assert (tmp_path / "state.json").exists()

    assert cli.main(["-q", "-c", str(config), "sync", "--dry-run", "--direction", "both"]) == 0
    assert "unchanged 1" in capsys.readouterr().out


def test_cli_describe_plan(tmp_path, capsys, monkeypatch):
    config = tmp_path / "c.yml"
    config.write_text(
        "graph: {access_token: t}\n"
        "syncs: [{source: {provider: github, repository: a/b}, plan_id: plan-1}]\n",
        encoding="utf-8",
    )
    planner = FakePlanner()
    planner.add_bucket("To do")
    planner.plan_details["categoryDescriptions"] = {"category2": "bug"}
    monkeypatch.setattr(cli.GraphClient, "from_config", classmethod(lambda cls, *a, **k: planner))
    assert cli.main(["-c", str(config), "describe-plan", "plan-1"]) == 0
    out = capsys.readouterr().out
    assert "To do" in out
    assert "category2   Red          bug" in out
