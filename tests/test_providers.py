from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
import responses
from responses import matchers

from git2planner.config import SourceConfig
from git2planner.errors import AttachmentError
from git2planner.models import Attachment, IssueChanges
from git2planner.providers import (
    ForgejoProvider,
    GiteaProvider,
    GitHubProvider,
    GitLabProvider,
    create_provider,
)

GH = "https://api.github.com"
GITEA = "https://gitea.test"
GITLAB = "https://gitlab.com/api/v4"
PROJECT = "group%2Fsub%2Fproject"


def gh_issue(number: int, **extra):
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": extra.pop("body", None),
        "state": extra.pop("state", "open"),
        "html_url": f"https://github.com/acme/app/issues/{number}",
        "repository_url": "https://api.github.com/repos/acme/app",
        "labels": [{"name": "bug", "color": "D73A4A"}],
        "assignees": [{"login": "octocat"}],
        "milestone": {"title": "v1", "due_on": "2026-05-01T07:00:00Z"},
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "user": {"login": "reporter"},
        **extra,
    }


def github(**config) -> GitHubProvider:
    return GitHubProvider(
        SourceConfig(provider="github", repository="acme/app", token="t", **config)
    )


# --------------------------------------------------------------------------- GitHub


@responses.activate
def test_github_lists_issues_and_skips_pull_requests():
    body = "See ![shot](https://github.com/user-attachments/assets/1b2c) and https://example.com"
    responses.get(
        f"{GH}/repos/acme/app/issues",
        json=[gh_issue(1, body=body), gh_issue(2, pull_request={"url": "x"})],
        headers={"Link": f'<{GH}/repos/acme/app/issues?page=2>; rel="next"'},
        match=[
            matchers.header_matcher(
                {"Authorization": "Bearer t", "X-GitHub-Api-Version": "2022-11-28"}
            )
        ],
    )
    responses.get(f"{GH}/repos/acme/app/issues?page=2", json=[gh_issue(3, state="closed")])
    issues = list(github().iter_issues("all", datetime(2026, 1, 1, tzinfo=timezone.utc)))
    assert [issue.number for issue in issues] == [1, 3]
    first = issues[0]
    assert first.labels[0].color == "d73a4a"
    assert first.assignees[0].login == "octocat"
    assert first.milestone.due_on == datetime(2026, 5, 1, 7, tzinfo=timezone.utc)
    assert [a.url for a in first.attachments] == ["https://github.com/user-attachments/assets/1b2c"]
    assert first.attachments[0].name == "shot"
    assert issues[1].state == "closed"
    assert "since=2026-01-01T00%3A00%3A00Z" in responses.calls[0].request.url


@responses.activate
def test_github_get_issue_handles_deleted_and_transferred():
    responses.get(f"{GH}/repos/acme/app/issues/1", status=404, json={"message": "Not Found"})
    responses.get(
        f"{GH}/repos/acme/app/issues/2",
        json=gh_issue(2, repository_url="https://api.github.com/repos/other/place"),
    )
    responses.get(f"{GH}/repos/acme/app/issues/3", json=gh_issue(3))
    provider = github()
    assert provider.get_issue(1) is None
    assert provider.get_issue(2) is None
    assert provider.get_issue(3).number == 3


@responses.activate
def test_github_enterprise_urls_and_user_lookup():
    responses.get(
        "https://ghe.test/api/v3/users/octo", json={"email": "octo@contoso.com", "name": "Octo"}
    )
    provider = github(url="https://ghe.test")
    assert provider.api_url == "https://ghe.test/api/v3"
    assert provider.web_url == "https://ghe.test"
    assert provider.get_user("octo").email == "octo@contoso.com"
    assert provider.get_user("OCTO").name == "Octo"
    assert len(responses.calls) == 1
    assert provider.is_attachment_url("https://ghe.test/acme/app/files/12/log.txt")
    assert not provider.is_attachment_url("https://ghe.test/acme/app/issues/1")


@responses.activate
def test_github_update_and_create_issue():
    responses.get(f"{GH}/repos/acme/app/assignees/octocat", status=204)
    responses.get(f"{GH}/repos/acme/app/assignees/stranger", status=404)
    responses.patch(f"{GH}/repos/acme/app/issues/1", json=gh_issue(1, state="closed"))
    responses.post(f"{GH}/repos/acme/app/issues", json=gh_issue(9))
    provider = github()
    issue = provider.update_issue(
        1, IssueChanges(state="closed", labels=["bug", "ui"], assignees=["octocat", "stranger"])
    )
    assert issue.state == "closed"
    payload = json.loads(responses.calls[2].request.body)
    assert payload == {
        "state": "closed",
        "state_reason": "completed",
        "labels": ["bug", "ui"],
        "assignees": ["octocat"],
    }
    created = provider.create_issue(IssueChanges(title="New", body="Body"))
    assert created.number == 9
    assert json.loads(responses.calls[-1].request.body) == {"title": "New", "body": "Body"}


@responses.activate
def test_download_refuses_large_files_and_html():
    responses.get(
        "https://github.com/user-attachments/files/1/a.zip",
        body=b"x" * 20,
        headers={"Content-Length": "20"},
    )
    responses.get(
        "https://github.com/user-attachments/assets/2", body="<html>", content_type="text/html"
    )
    responses.get(
        "https://example.com/file.bin",
        body=b"data",
        match=[matchers.header_matcher({}, strict_match=False)],
    )
    provider = github()
    with pytest.raises(AttachmentError, match="limit"):
        provider.download(
            Attachment("a.zip", "https://github.com/user-attachments/files/1/a.zip"), 10
        )
    with pytest.raises(AttachmentError, match="HTML"):
        provider.download(Attachment("b", "https://github.com/user-attachments/assets/2"), 100)
    content, _ = provider.download(Attachment("f", "https://example.com/file.bin"), 100)
    assert content == b"data"
    assert "Authorization" not in responses.calls[-1].request.headers


# --------------------------------------------------------------------------- Gitea / Forgejo


def gitea_issue(number: int, **extra):
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": "Log: [trace.txt](https://gitea.test/attachments/0b1f3c2a-1111-2222-3333-444455556666)",
        "state": "open",
        "html_url": f"https://gitea.test/acme/app/issues/{number}",
        "labels": [{"name": "bug", "color": "ee0701"}],
        "assignees": [{"login": "alice", "email": "alice@contoso.com", "full_name": "Alice"}],
        "milestone": None,
        "due_date": "2026-03-01T00:00:00Z",
        "assets": [
            {
                "name": "screen.png",
                "browser_download_url": "https://gitea.test/attachments/abc",
                "size": 10,
            }
        ],
        "pull_request": None,
        **extra,
    }


def gitea(cls=GiteaProvider) -> GiteaProvider:
    provider = "forgejo" if cls is ForgejoProvider else "gitea"
    return cls(SourceConfig(provider=provider, repository="acme/app", url=f"{GITEA}/", token="t"))


@responses.activate
def test_gitea_pagination_assets_and_due_date():
    page1 = [gitea_issue(n) for n in range(1, 51)]
    responses.get(
        f"{GITEA}/api/v1/repos/acme/app/issues",
        json=page1,
        match=[
            matchers.query_param_matcher(
                {"state": "open", "limit": "50", "page": "1", "type": "issues"}
            )
        ],
    )
    responses.get(
        f"{GITEA}/api/v1/repos/acme/app/issues",
        json=[gitea_issue(51)],
        match=[
            matchers.query_param_matcher(
                {"state": "open", "limit": "50", "page": "2", "type": "issues"}
            )
        ],
    )
    issues = list(gitea().iter_issues("open"))
    assert len(issues) == 51
    issue = issues[0]
    assert issue.due_date == datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert issue.assignees[0].email == "alice@contoso.com"
    assert [a.name for a in issue.attachments] == ["screen.png", "trace.txt"]
    assert responses.calls[0].request.headers["Authorization"] == "token t"


@responses.activate
def test_gitea_update_creates_missing_labels():
    responses.get(f"{GITEA}/api/v1/repos/acme/app/labels", json=[{"id": 1, "name": "bug"}])
    responses.get(f"{GITEA}/api/v1/orgs/acme/labels", status=404, json={})
    responses.post(f"{GITEA}/api/v1/repos/acme/app/labels", json={"id": 7, "name": "ui"})
    responses.put(f"{GITEA}/api/v1/repos/acme/app/issues/1/labels", json=[])
    responses.patch(f"{GITEA}/api/v1/repos/acme/app/issues/1", json=gitea_issue(1))
    responses.get(f"{GITEA}/api/v1/repos/acme/app/issues/1", json=gitea_issue(1, title="Renamed"))
    issue = gitea().update_issue(
        1,
        IssueChanges(
            title="Renamed",
            labels=["bug", "ui"],
            clear_due_date=True,
            label_colors={"ui": "0078d4"},
        ),
    )
    assert issue.title == "Renamed"
    label_body = json.loads(responses.calls[2].request.body)
    assert label_body == {"name": "ui", "color": "#0078d4"}
    assert json.loads(responses.calls[3].request.body) == {"labels": [1, 7]}
    assert json.loads(responses.calls[4].request.body) == {
        "title": "Renamed",
        "unset_due_date": True,
    }


@responses.activate
def test_forgejo_create_issue_closed():
    responses.post(f"{GITEA}/api/v1/repos/acme/app/issues", json=gitea_issue(5, state="closed"))
    provider = gitea(ForgejoProvider)
    assert provider.display_name == "Forgejo"
    issue = provider.create_issue(
        IssueChanges(
            title="T", state="closed", due_date=datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
        )
    )
    assert issue.state == "closed"
    assert json.loads(responses.calls[0].request.body) == {
        "title": "T",
        "body": "",
        "due_date": "2026-01-02T12:00:00Z",
        "closed": True,
    }
    assert provider.is_attachment_url(
        "https://gitea.test/attachments/0b1f3c2a-1111-2222-3333-444455556666"
    )


# --------------------------------------------------------------------------- GitLab


def gl_issue(iid: int, **extra):
    return {
        "iid": iid,
        "title": f"Issue {iid}",
        "description": "Crash ![log](/uploads/0123456789abcdef0123456789abcdef/crash.log)",
        "state": "opened",
        "web_url": f"https://gitlab.com/group/sub/project/-/issues/{iid}",
        "labels": [{"name": "bug", "color": "#d9534f"}],
        "assignees": [{"username": "dev", "name": "Dev"}],
        "milestone": {"title": "Sprint 1", "due_date": "2026-04-30"},
        "due_date": None,
        "author": {"username": "reporter"},
        **extra,
    }


def gitlab() -> GitLabProvider:
    return GitLabProvider(
        SourceConfig(provider="gitlab", repository="group/sub/project", token="t")
    )


@responses.activate
def test_gitlab_lists_issues_with_absolute_uploads():
    responses.get(
        f"{GITLAB}/projects/{PROJECT}/issues",
        json=[gl_issue(4)],
        match=[matchers.header_matcher({"PRIVATE-TOKEN": "t"})],
    )
    (issue,) = list(gitlab().iter_issues("open"))
    assert issue.number == 4
    assert issue.state == "open"
    assert issue.labels[0].color == "d9534f"
    assert issue.milestone.due_on == datetime(2026, 4, 30, tzinfo=timezone.utc)
    upload = (
        "https://gitlab.com/group/sub/project/uploads/0123456789abcdef0123456789abcdef/crash.log"
    )
    assert upload in issue.body
    assert [a.url for a in issue.attachments] == [upload]
    request = responses.calls[0].request
    assert f"/projects/{PROJECT}/issues" in request.url
    assert "state=opened" in request.url
    assert "with_labels_details=true" in request.url


@responses.activate
def test_gitlab_update_issue_and_users():
    responses.get(f"{GITLAB}/users", json=[{"id": 42, "username": "dev"}])
    responses.get(f"{GITLAB}/users/42", json={"public_email": "dev@contoso.com", "name": "Dev"})
    responses.put(
        f"{GITLAB}/projects/{PROJECT}/issues/4",
        json=gl_issue(4, state="closed", labels=["bug", "ui"]),
    )
    provider = gitlab()
    assert provider.get_user("dev").email == "dev@contoso.com"
    issue = provider.update_issue(
        4,
        IssueChanges(
            state="closed",
            labels=["bug", "ui"],
            assignees=["dev"],
            due_date=datetime(2026, 5, 2, 12, tzinfo=timezone.utc),
        ),
    )
    assert issue.state == "closed"
    assert [label.name for label in issue.labels] == ["bug", "ui"]
    payload = json.loads(responses.calls[-1].request.body)
    assert payload == {
        "state_event": "close",
        "labels": "bug,ui",
        "assignee_ids": [42],
        "due_date": "2026-05-02",
    }


@responses.activate
def test_gitlab_download_prefers_api_route():
    api = f"{GITLAB}/projects/{PROJECT}/uploads/0123456789abcdef0123456789abcdef/crash.log"
    responses.get(api, body=b"log", match=[matchers.header_matcher({"PRIVATE-TOKEN": "t"})])
    url = "https://gitlab.com/group/sub/project/uploads/0123456789abcdef0123456789abcdef/crash.log"
    content, _ = gitlab().download(Attachment("crash.log", url), 100)
    assert content == b"log"


def test_factory_and_project_url():
    provider = create_provider(
        SourceConfig(provider="gitlab", repository="g/p", url="https://gl.test/api/v4")
    )
    assert isinstance(provider, GitLabProvider)
    assert provider.project_web_url == "https://gl.test/g/p"
    assert provider.api_url == "https://gl.test/api/v4"
