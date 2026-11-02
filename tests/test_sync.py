from __future__ import annotations

from datetime import datetime, timezone

from git2planner.attachments import reference_key
from git2planner.models import GitUser, Label, Milestone
from git2planner.state import StateStore
from git2planner.sync import due_day, due_value, extract_title, matches_filters, strip_footer

from .conftest import make_config
from .fakes import FakePlanner, FakeProvider

ISSUE_URL = "https://git.example.com/acme/app/issues/{}"


def only_task(planner: FakePlanner) -> tuple[str, dict, dict]:
    assert len(planner.tasks) == 1
    task_id = next(iter(planner.tasks))
    return task_id, planner.tasks[task_id], planner.details[task_id]


def bucket_name(planner: FakePlanner, bucket_id: str) -> str:
    return planner.buckets[bucket_id]["name"]


def seed(planner: FakePlanner, provider: FakeProvider) -> None:
    planner.add_user("u-alice", "alice@contoso.com")
    planner.add_user("u-bob", "bob@contoso.com")
    provider.profiles["alice"] = GitUser("alice", email="alice@contoso.com")
    provider.add(
        1,
        "Login fails",
        body=(
            "Steps to reproduce:\n\n- [ ] open the page\n- [x] click **Login**\n\n"
            "![screenshot](https://git.example.com/attachments/1234)"
        ),
        labels=[Label("bug", "d73a4a"), Label("priority/high")],
        assignees=["alice"],
        attachments=[],
    )
    provider.issues[1].attachments = provider.body_attachments(provider.issues[1].body)


PRIORITY = {"mapping": {"priority/high": "important"}}
CATEGORIES = {"exclude": ["priority/high"]}


# --------------------------------------------------------------------------- git -> planner


def test_creates_task_with_all_mapped_fields(planner, provider, run):
    seed(planner, provider)
    report = run(make_config(priority=PRIORITY, categories=CATEGORIES))

    assert report.ok, report.errors
    assert report.created == 1
    _, task, details = only_task(planner)
    assert task["title"] == "#1 Login fails"
    assert task["priority"] == 3
    assert task["percentComplete"] == 0
    assert set(task["assignments"]) == {"u-alice"}
    assert task["appliedCategories"] == {"category2": True}
    assert planner.plan_details["categoryDescriptions"] == {"category2": "bug"}
    assert bucket_name(planner, task["bucketId"]) == "To do"

    assert details["description"].startswith("Steps to reproduce:")
    assert "open the page" not in details["description"]
    assert details["description"].endswith(f"Synced from GitHub acme/app#1: {ISSUE_URL.format(1)}")
    titles = sorted((item["title"], item["isChecked"]) for item in details["checklist"].values())
    assert titles == [("click Login", True), ("open the page", False)]
    assert reference_key(ISSUE_URL.format(1)) in details["references"]
    assert reference_key("https://git.example.com/attachments/1234") in details["references"]


def test_second_run_changes_nothing(planner, provider, run):
    seed(planner, provider)
    run()
    planner.calls.clear()
    report = run()
    assert report.unchanged == 1
    assert report.created == report.updated == 0
    assert planner.writes() == []


def test_only_changed_fields_are_sent(planner, provider, run):
    seed(planner, provider)
    run()
    planner.calls.clear()
    provider.issues[1].title = "Login fails on Safari"
    report = run()
    assert report.updated == 1
    ((_name, (_, patch)),) = [call for call in planner.calls if call[0] == "update_task"]
    assert patch == {"title": "#1 Login fails on Safari"}
    assert not [call for call in planner.calls if call[0] == "update_task_details"]


def test_close_completes_and_moves_to_closed_bucket_then_reopen(planner, provider, run):
    seed(planner, provider)
    run()
    provider.issues[1].state = "closed"
    report = run()
    _, task, _ = only_task(planner)
    assert report.completed == 1
    assert task["percentComplete"] == 100
    assert bucket_name(planner, task["bucketId"]) == "Done"

    provider.issues[1].state = "open"
    report = run()
    _, task, _ = only_task(planner)
    assert report.reopened == 1
    assert task["percentComplete"] == 0
    assert bucket_name(planner, task["bucketId"]) == "To do"


def test_planner_edits_survive_until_the_issue_changes(planner, provider, run):
    seed(planner, provider)
    run()
    task_id, task, _ = only_task(planner)
    task["title"] = "Renamed in Planner"
    task["priority"] = 1
    other = planner.add_bucket("Doing")
    task["bucketId"] = other

    assert run().unchanged == 1
    assert planner.tasks[task_id]["title"] == "Renamed in Planner"

    provider.issues[1].title = "New title from Git"
    run()
    assert planner.tasks[task_id]["title"] == "#1 New title from Git"
    assert planner.tasks[task_id]["priority"] == 1
    assert planner.tasks[task_id]["bucketId"] == other


def test_removed_assignee_and_label_are_unset(planner, provider, run):
    seed(planner, provider)
    run(make_config(categories=CATEGORIES))
    provider.issues[1].assignees = []
    provider.issues[1].labels = [Label("priority/high")]
    run(make_config(categories=CATEGORIES))
    _, task, _ = only_task(planner)
    assert task["assignments"] == {}
    assert task["appliedCategories"] == {}


def test_checklist_change_only_patches_details(planner, provider, run):
    seed(planner, provider)
    run()
    planner.calls.clear()
    provider.issues[1].body = provider.issues[1].body.replace("- [ ] open", "- [x] open")
    run()
    assert list(planner.writes()) == ["update_task_details"]
    _, _, details = only_task(planner)
    assert all(item["isChecked"] for item in details["checklist"].values())


def test_on_close_delete_and_recreate_on_reopen(planner, provider, run):
    seed(planner, provider)
    config = make_config(on_close="delete")
    run(config)
    provider.issues[1].state = "closed"
    assert run(config).deleted == 1
    assert planner.tasks == {}
    provider.issues[1].state = "open"
    assert run(config).created == 1
    assert len(planner.tasks) == 1


def test_task_deleted_in_planner_is_not_recreated_by_default(planner, provider, run):
    seed(planner, provider)
    run()
    planner.tasks.clear()
    planner.details.clear()
    provider.issues[1].title = "changed"
    report = run()
    assert report.skipped == 1
    assert report.warnings
    assert planner.tasks == {}
    assert run().skipped == 1


def test_task_deleted_in_planner_is_recreated_when_enabled(planner, provider, run):
    seed(planner, provider)
    config = make_config(recreate_deleted=True)
    run(config)
    planner.tasks.clear()
    planner.details.clear()
    provider.issues[1].title = "changed"
    assert run(config).created == 1
    assert len(planner.tasks) == 1


def test_dry_run_writes_nothing(planner, provider, run, store):
    seed(planner, provider)
    report = run(dry_run=True)
    assert report.created == 1
    assert report.dry_run
    assert planner.writes() == []
    assert not store.path.exists()


def test_lost_state_links_existing_tasks_instead_of_duplicating(planner, provider, run, tmp_path):
    seed(planner, provider)
    run()
    fresh = StateStore(tmp_path / "other.json")
    report = run(state=fresh)
    assert report.linked == 1
    assert report.created == 0
    assert len(planner.tasks) == 1


def test_filters_gate_new_tasks_only(planner, provider, run):
    provider.add(1, "bug", labels=["sync-me"])
    provider.add(2, "no label")
    provider.add(3, "closed one", labels=["sync-me"], state="closed")
    provider.add(4, "pull request", labels=["sync-me"], is_pull_request=True)
    config = make_config(filters={"labels": ["sync-me"]})
    report = run(config)
    assert report.created == 1
    provider.issues[1].labels = []
    report = run(config)
    assert report.created == 0
    assert report.updated == 1
    assert len(planner.tasks) == 1


def test_missing_issue_policy_delete(planner, provider, run):
    provider.add(1, "temp")
    config = make_config(on_missing="delete")
    run(config)
    del provider.issues[1]
    report = run(config)
    assert report.deleted == 1
    assert planner.tasks == {}


def test_missing_issue_kept_by_default(planner, provider, run, store):
    provider.add(1, "temp")
    run()
    del provider.issues[1]
    report = run()
    assert report.warnings
    assert len(planner.tasks) == 1
    assert store.sync("app").issues["1"].missing


def test_upload_mode_stores_files_in_group_library(planner, provider, run, store):
    seed(planner, provider)
    report = run(make_config(attachments={"mode": "upload"}))
    assert report.ok, report.errors
    assert list(planner.files) == ["git2planner/acme/app/1/1234.png"]
    _, _, details = only_task(planner)
    keys = list(details["references"])
    assert any("sharepoint" in key for key in keys)
    planner.calls.clear()
    run(make_config(attachments={"mode": "upload"}))
    assert ("upload_group_file", "git2planner/acme/app/1/1234.png") not in planner.calls


def test_label_bucket_strategy_and_milestone_due_date(planner, provider, run):
    due = datetime(2026, 5, 1, tzinfo=timezone.utc)
    provider.add(1, "fix", labels=["backend"], milestone=Milestone("v1", due))
    config = make_config(buckets={"strategy": "label", "mapping": {"backend": "Backend"}})
    run(config)
    _, task, _ = only_task(planner)
    assert bucket_name(planner, task["bucketId"]) == "Backend"
    assert task["dueDateTime"] == "2026-05-01T12:00:00Z"


def test_single_issue_run(planner, provider, run):
    provider.add(1, "one")
    provider.add(2, "two")
    report = run(issues=[2])
    assert report.created == 1
    (_, task, _) = only_task(planner)
    assert task["title"] == "#2 two"


# --------------------------------------------------------------------------- planner -> git

BOTH = {"direction": "both"}


def test_planner_title_edit_goes_to_git_without_bouncing(planner, provider, run):
    seed(planner, provider)
    run()
    task_id, task, _ = only_task(planner)
    task["title"] = "#1 Login fails on Safari"
    planner.calls.clear()

    report = run(make_config(**BOTH))
    assert report.pulled == 1
    assert provider.issues[1].title == "Login fails on Safari"
    assert planner.tasks[task_id]["title"] == "#1 Login fails on Safari"
    assert not [call for call in planner.calls if call[0] == "update_task"]

    report = run(make_config(**BOTH))
    assert report.pulled == 0
    assert report.unchanged == 1


def test_planner_completion_closes_issue(planner, provider, run):
    seed(planner, provider)
    run()
    task_id, task, _ = only_task(planner)
    task["percentComplete"] = 100
    run(make_config(**BOTH))
    assert provider.issues[1].state == "closed"
    assert bucket_name(planner, planner.tasks[task_id]["bucketId"]) == "Done"


def test_moving_task_to_closed_bucket_closes_issue_and_ticks_task(planner, provider, run):
    seed(planner, provider)
    run()
    done = planner.add_bucket("Done")
    task_id, task, _ = only_task(planner)
    task["bucketId"] = done
    run(make_config(**BOTH))
    assert provider.issues[1].state == "closed"
    assert planner.tasks[task_id]["percentComplete"] == 100


def test_planner_category_and_assignee_become_label_and_assignee(planner, provider, run):
    seed(planner, provider)
    planner.plan_details["categoryDescriptions"] = {"category5": "ui"}
    config = make_config(
        categories=CATEGORIES, assignees={"mapping": {"bob": "bob@contoso.com"}}, **BOTH
    )
    run(config)
    _task_id, task, _ = only_task(planner)
    task["appliedCategories"]["category5"] = True
    task["assignments"]["u-bob"] = {"orderHint": " !"}

    report = run(config)
    issue = provider.issues[1]
    assert report.pulled == 1
    assert "ui" in issue.label_names
    assert {user.login for user in issue.assignees} == {"alice", "bob"}
    assert run(config).pulled == 0


def test_planner_checklist_toggle_and_new_item(planner, provider, run):
    seed(planner, provider)
    run()
    task_id, _, details = only_task(planner)
    for item in details["checklist"].values():
        if item["title"] == "open the page":
            item["isChecked"] = True
    details["checklist"]["planner-guid"] = {"title": "write a test", "isChecked": False}

    run(make_config(**BOTH))
    body = provider.issues[1].body
    assert "- [x] open the page" in body
    assert "- [ ] write a test" in body
    checklist = planner.details[task_id]["checklist"]
    assert "planner-guid" not in checklist
    assert sorted(item["title"] for item in checklist.values()) == [
        "click Login",
        "open the page",
        "write a test",
    ]
    assert run(make_config(**BOTH)).pulled == 0


def test_conflict_keeps_git_value_by_default(planner, provider, run):
    seed(planner, provider)
    run()
    task_id, task, _ = only_task(planner)
    task["title"] = "#1 Planner title"
    provider.issues[1].title = "Git title"
    report = run(make_config(**BOTH))
    assert report.conflicts == 1
    assert provider.issues[1].title == "Git title"
    assert planner.tasks[task_id]["title"] == "#1 Git title"


def test_conflict_keeps_planner_value_when_configured(planner, provider, run):
    seed(planner, provider)
    run()
    task_id, task, _ = only_task(planner)
    task["title"] = "#1 Planner title"
    provider.issues[1].title = "Git title"
    run(make_config(conflicts="planner", **BOTH))
    assert provider.issues[1].title == "Planner title"
    assert planner.tasks[task_id]["title"] == "#1 Planner title"


def test_planner_due_date_is_pulled_and_stays_stable(planner, provider, run):
    seed(planner, provider)
    run()
    task_id, task, _ = only_task(planner)
    task["dueDateTime"] = "2026-03-10T23:00:00Z"
    run(make_config(**BOTH))
    assert provider.issues[1].due_date == datetime(2026, 3, 11, 12, tzinfo=timezone.utc)
    report = run(make_config(**BOTH))
    assert report.pulled == 0
    assert planner.tasks[task_id]["dueDateTime"] == "2026-03-10T23:00:00Z"


def test_new_planner_tasks_become_issues_once(planner, provider, run):
    planner.add_bucket("To do")
    inbox = planner.add_bucket("Inbox")
    planner.add_task(
        "Order new laptops",
        bucketId=inbox,
        details={
            "description": "For the new hires",
            "checklist": {"g1": {"title": "get quotes", "isChecked": False, "orderHint": "1"}},
        },
    )
    planner.add_task("Personal reminder", bucketId=next(iter(planner.buckets)))
    config = make_config(planner_to_git={"create_issues": True, "buckets": ["Inbox"]}, **BOTH)

    report = run(config)
    assert report.ok, report.errors
    assert report.issues_created == 1
    issue = provider.issues[1]
    assert issue.title == "Order new laptops"
    assert issue.body == "For the new hires\n\n- [ ] get quotes"
    task = next(t for t in planner.tasks.values() if t["bucketId"] == inbox)
    assert task["title"] == "#1 Order new laptops"
    details = planner.details[task["id"]]
    assert reference_key(issue.url) in details["references"]
    assert [item["title"] for item in details["checklist"].values()] == ["get quotes"]

    report = run(config)
    assert report.issues_created == 0
    assert len(provider.issues) == 1


def test_planner_to_git_only_does_not_push(planner, provider, run):
    seed(planner, provider)
    run()
    task_id, task, _ = only_task(planner)
    provider.issues[1].title = "Changed in Git"
    task["percentComplete"] = 100
    run(make_config(direction="planner-to-git"))
    assert provider.issues[1].state == "closed"
    assert planner.tasks[task_id]["title"] == "#1 Login fails"


def test_cli_direction_override(planner, provider, run):
    seed(planner, provider)
    run()
    _, task, _ = only_task(planner)
    task["percentComplete"] = 100
    run(direction="pull")
    assert provider.issues[1].state == "closed"


def test_deleted_task_closes_issue_when_enabled(planner, provider, run):
    seed(planner, provider)
    run()
    planner.tasks.clear()
    planner.details.clear()
    run(make_config(planner_to_git={"close_deleted": True}, **BOTH))
    assert provider.issues[1].state == "closed"


# --------------------------------------------------------------------------- helpers


def test_extract_title_variants():
    provider = FakeProvider()
    issue = provider.add(12, "x")
    assert extract_title("#{number} {title}", "#12 Fix login", issue, provider) == "Fix login"
    assert extract_title("#{number} {title}", "#12Fix login", issue, provider) == "Fix login"
    assert extract_title("#{number} {title}", "Fix login", issue, provider) == "Fix login"
    assert extract_title("[{repo}] {title}", "[app] Crash", issue, provider) == "Crash"
    assert extract_title("{title}", "Plain", issue, provider) == "Plain"


def test_strip_footer():
    text = "Body\n\n---\nSynced from GitHub acme/app#1: https://x/1"
    assert strip_footer(text) == "Body"
    assert strip_footer("---\nSynced from GitHub acme/app#1: https://x/1") == ""
    assert strip_footer("No footer") == "No footer"


def test_due_helpers():
    assert due_value(datetime(2026, 1, 2, tzinfo=timezone.utc)) == "2026-01-02T12:00:00Z"
    assert due_value(datetime(2026, 1, 2, 7, tzinfo=timezone.utc)) == "2026-01-02T07:00:00Z"
    assert due_day("2026-01-01T23:00:00Z") == "2026-01-02"
    assert due_day("2026-01-02T12:00:00Z") == "2026-01-02"
    assert due_day("2026-01-02T05:00:00Z") == "2026-01-02"
    assert due_day(None) is None


def test_matches_filters_created_after_and_milestone():
    provider = FakeProvider()
    config = make_config(
        filters={"created_after": "2025-01-01", "milestones": ["v1"], "state": "all"}
    ).syncs[0]
    old = provider.add(1, "old", created_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
    new = provider.add(2, "new", created_at=datetime(2025, 6, 1, tzinfo=timezone.utc))
    assert not matches_filters(old, config.filters)
    assert not matches_filters(new, config.filters)
    new.milestone = Milestone("V1")
    assert matches_filters(new, config.filters)


def test_tasks_of_other_syncs_never_become_issues(planner, store):
    from git2planner.config import SourceConfig, parse_config
    from git2planner.sync import SyncEngine

    first = FakeProvider()
    second = FakeProvider(SourceConfig(provider="github", repository="acme/other"))
    first.add(1, "From the first repository")
    sync = {"plan_id": "plan-1", "direction": "both", "planner_to_git": {"create_issues": True}}
    config = parse_config(
        {
            "graph": {"access_token": "t"},
            "syncs": [
                {"name": "b", "source": {"provider": "github", "repository": "acme/other"}, **sync},
                {"name": "a", "source": {"provider": "github", "repository": "acme/app"}, **sync},
            ],
        },
        environ={},
    )

    def factory(source):
        return first if source.repository == "acme/app" else second

    for _ in range(2):
        reports = SyncEngine(config, planner, store, provider_factory=factory).run()
        assert all(report.ok for report in reports)
    assert second.issues == {}
    assert len(planner.tasks) == 1

    lost = StateStore(None)
    (report,) = SyncEngine(config, planner, lost, provider_factory=factory).run(["b"])
    assert report.issues_created == 0
