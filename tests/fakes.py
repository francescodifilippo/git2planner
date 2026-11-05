"""In-memory doubles for Microsoft Graph and a Git provider."""

from __future__ import annotations

import copy
import itertools
from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import Any

from git2planner.config import SourceConfig
from git2planner.errors import NotFoundError
from git2planner.graph import GraphClient
from git2planner.http import HttpClient
from git2planner.models import Attachment, GitUser, Issue, IssueChanges, Label
from git2planner.providers.base import Board, IssueProvider

GROUP_ID = "group-1"
PLAN_ID = "plan-1"


def _merge_open_type(target: dict[str, Any], patch: Mapping[str, Any]) -> None:
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            target[key].update(value)
        else:
            target[key] = copy.deepcopy(value)


class FakePlanner:
    """Implements the subset of :class:`GraphClient` used by the engine."""

    plan_group_id = staticmethod(GraphClient.plan_group_id)

    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self._versions = itertools.count(1)
        self.plan = {
            "id": PLAN_ID,
            "title": "Engineering",
            "container": {"containerId": GROUP_ID, "type": "group"},
        }
        self.plan_details: dict[str, Any] = {"categoryDescriptions": {}, "@odata.etag": "pd-0"}
        self.buckets: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.details: dict[str, dict[str, Any]] = {}
        self.users: dict[str, dict[str, Any]] = {}
        self.members: set[str] = set()
        self.files: dict[str, bytes] = {}
        self.calls: list[tuple[str, Any]] = []

    # helpers ---------------------------------------------------------------
    def _etag(self) -> str:
        return f'W/"{next(self._versions)}"'

    def add_bucket(self, name: str) -> str:
        bucket_id = f"bucket-{next(self._ids)}"
        self.buckets[bucket_id] = {"id": bucket_id, "name": name, "planId": PLAN_ID}
        return bucket_id

    def add_user(
        self, object_id: str, upn: str, mail: str | None = None, *, member: bool = True
    ) -> None:
        self.users[object_id] = {"id": object_id, "userPrincipalName": upn, "mail": mail or upn}
        if member:
            self.members.add(object_id)

    def add_task(self, title: str, **fields: Any) -> str:
        task_id = f"task-{next(self._ids)}"
        details = fields.pop("details", {})
        self.tasks[task_id] = {
            "id": task_id,
            "planId": PLAN_ID,
            "title": title,
            "bucketId": fields.pop("bucketId", next(iter(self.buckets), None)),
            "percentComplete": 0,
            "priority": 5,
            "assignments": {},
            "appliedCategories": {},
            "@odata.etag": self._etag(),
            **fields,
        }
        self.details[task_id] = {
            "id": task_id,
            "description": "",
            "references": {},
            "checklist": {},
            "previewType": "automatic",
            "@odata.etag": self._etag(),
            **details,
        }
        return task_id

    def writes(self) -> list[str]:
        return [
            name for name, _ in self.calls if not name.startswith(("get", "list", "batch", "find"))
        ]

    # plans -------------------------------------------------------------------
    def get_plan(self, plan_id: str) -> dict[str, Any]:
        self.calls.append(("get_plan", plan_id))
        return copy.deepcopy(self.plan)

    def get_plan_details(self, plan_id: str) -> dict[str, Any]:
        return copy.deepcopy(self.plan_details)

    def update_plan_details(
        self, plan_id: str, patch: Mapping[str, Any], etag: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("update_plan_details", copy.deepcopy(dict(patch))))
        _merge_open_type(self.plan_details, patch)
        self.plan_details["@odata.etag"] = self._etag()
        return copy.deepcopy(self.plan_details)

    def list_buckets(self, plan_id: str) -> list[dict[str, Any]]:
        return [copy.deepcopy(bucket) for bucket in self.buckets.values()]

    def create_bucket(self, plan_id: str, name: str) -> dict[str, Any]:
        self.calls.append(("create_bucket", name))
        return copy.deepcopy(self.buckets[self.add_bucket(name)])

    # tasks -------------------------------------------------------------------
    def list_tasks(self, plan_id: str) -> list[dict[str, Any]]:
        return [copy.deepcopy(task) for task in self.tasks.values()]

    def get_task(self, task_id: str) -> dict[str, Any]:
        if task_id not in self.tasks:
            raise NotFoundError("task not found", status=404)
        return copy.deepcopy(self.tasks[task_id])

    def create_task(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(("create_task", copy.deepcopy(dict(payload))))
        data = copy.deepcopy(dict(payload))
        task_id = self.add_task(data.pop("title"), bucketId=data.pop("bucketId"))
        task = self.tasks[task_id]
        _merge_open_type(task, data)
        task["assignments"] = {k: v for k, v in task.get("assignments", {}).items() if v}
        task["appliedCategories"] = {
            k: v for k, v in task.get("appliedCategories", {}).items() if v
        }
        return copy.deepcopy(task)

    def update_task(
        self, task_id: str, patch: Mapping[str, Any], etag: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("update_task", (task_id, copy.deepcopy(dict(patch)))))
        if task_id not in self.tasks:
            raise NotFoundError("task not found", status=404)
        task = self.tasks[task_id]
        for key, value in patch.items():
            if key in ("assignments", "appliedCategories"):
                bag = task.setdefault(key, {})
                for item, flag in value.items():
                    if flag in (None, False):
                        bag.pop(item, None)
                    else:
                        bag[item] = flag
            else:
                task[key] = value
        task["@odata.etag"] = self._etag()
        return copy.deepcopy(task)

    def delete_task(self, task_id: str, etag: str | None = None) -> None:
        self.calls.append(("delete_task", task_id))
        if task_id not in self.tasks:
            raise NotFoundError("task not found", status=404)
        del self.tasks[task_id]
        del self.details[task_id]

    def get_task_details(self, task_id: str, *, retries: int = 4) -> dict[str, Any]:
        if task_id not in self.details:
            raise NotFoundError("details not found", status=404)
        return copy.deepcopy(self.details[task_id])

    def update_task_details(
        self, task_id: str, patch: Mapping[str, Any], etag: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("update_task_details", (task_id, copy.deepcopy(dict(patch)))))
        if task_id not in self.details:
            raise NotFoundError("details not found", status=404)
        details = self.details[task_id]
        for key, value in patch.items():
            if key in ("references", "checklist"):
                bag = details.setdefault(key, {})
                for item, item_value in value.items():
                    if item_value is None:
                        bag.pop(item, None)
                    else:
                        bag.setdefault(item, {}).update(copy.deepcopy(item_value))
            else:
                details[key] = value
        details["@odata.etag"] = self._etag()
        return copy.deepcopy(details)

    def batch_get(self, paths: list[str]) -> dict[str, tuple[int, dict[str, Any]]]:
        self.calls.append(("batch_get", list(paths)))
        result: dict[str, tuple[int, dict[str, Any]]] = {}
        for path in paths:
            task_id = path.split("/")[3]
            if task_id in self.details:
                result[path] = (200, copy.deepcopy(self.details[task_id]))
            else:
                result[path] = (404, {})
        return result

    # directory ---------------------------------------------------------------
    def get_user(self, identifier: str) -> dict[str, Any] | None:
        ident = identifier.lower()
        for user in self.users.values():
            if ident in (user["id"].lower(), user["userPrincipalName"].lower()):
                return copy.deepcopy(user)
        return None

    def find_user_by_email(self, email: str) -> dict[str, Any] | None:
        for user in self.users.values():
            if (user.get("mail") or "").lower() == email.lower():
                return copy.deepcopy(user)
        return None

    def list_group_member_ids(self, group_id: str) -> set[str]:
        return set(self.members)

    def get_group_drive(self, group_id: str) -> dict[str, Any]:
        return {"webUrl": "https://contoso.sharepoint.com/sites/eng/Shared%20Documents"}

    def upload_group_file(
        self, group_id: str, path: str, content: bytes, content_type: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("upload_group_file", path))
        self.files[path] = content
        return {"webUrl": f"https://contoso.sharepoint.com/sites/eng/Shared%20Documents/{path}"}


class FakeBoard(Board):
    def __init__(self, columns: list[str], items: dict[int, str] | None = None) -> None:
        super().__init__("Roadmap", columns)
        self.items = dict(items or {})
        self.moves: list[tuple[int, str | None]] = []

    def column_of(self, issue: Issue) -> str | None:
        return self.items.get(issue.number)

    def move(self, issue: Issue, column: str | None) -> Issue:
        self.moves.append((issue.number, column))
        if column is None:
            self.items.pop(issue.number, None)
        else:
            self.items[issue.number] = column
        return issue


class FakeProvider(IssueProvider):
    kind = "github"
    display_name = "GitHub"
    supports_due_date = True

    def __init__(self, config: SourceConfig | None = None) -> None:
        config = config or SourceConfig(provider="github", repository="acme/app")
        super().__init__(config, HttpClient("https://git.example.com/api"))
        self.issues: dict[int, Issue] = {}
        self.profiles: dict[str, GitUser] = {}
        self.updates: list[tuple[int, IssueChanges]] = []
        self.created: list[IssueChanges] = []
        self.downloads: dict[str, bytes] = {}
        self.board: FakeBoard | None = None

    def load_board(self, config: Any) -> Board:
        assert self.board is not None
        return self.board

    @property
    def web_url(self) -> str:
        return "https://git.example.com"

    def _create_http(self) -> HttpClient:  # pragma: no cover - not used
        return HttpClient("https://git.example.com/api")

    def add(self, number: int, title: str, **fields: Any) -> Issue:
        labels = [
            label if isinstance(label, Label) else Label(label)
            for label in fields.pop("labels", [])
        ]
        assignees = [
            user if isinstance(user, GitUser) else GitUser(user)
            for user in fields.pop("assignees", [])
        ]
        issue = Issue(
            number=number,
            title=title,
            body=fields.pop("body", ""),
            state=fields.pop("state", "open"),
            url=f"https://git.example.com/acme/app/issues/{number}",
            repository="acme/app",
            labels=labels,
            assignees=assignees,
            **fields,
        )
        self.issues[number] = issue
        return issue

    def iter_issues(
        self,
        state: str = "all",
        since: datetime | None = None,
        *,
        include_pull_requests: bool = False,
    ) -> Iterator[Issue]:
        for issue in list(self.issues.values()):
            if state not in ("all", issue.state):
                continue
            if since is not None and issue.updated_at is not None and issue.updated_at < since:
                continue
            if issue.is_pull_request and not include_pull_requests:
                continue
            yield copy.deepcopy(issue)

    def get_issue(self, number: int) -> Issue | None:
        issue = self.issues.get(number)
        return copy.deepcopy(issue) if issue else None

    def _fetch_user(self, login: str) -> GitUser | None:
        return self.profiles.get(login.lower())

    def check_access(self) -> str:
        return "acme/app (private)"

    def is_attachment_url(self, url: str) -> bool:
        return "/attachments/" in url

    def download(self, attachment: Attachment, max_bytes: int) -> tuple[bytes, str | None]:
        return self.downloads.get(attachment.url, b"data"), "image/png"

    def create_issue(self, changes: IssueChanges) -> Issue:
        self.created.append(changes)
        number = max(self.issues, default=0) + 1
        issue = self.add(
            number,
            changes.title or "",
            body=changes.body or "",
            labels=list(changes.labels or []),
            assignees=list(changes.assignees or []),
            state=changes.state or "open",
            due_date=changes.due_date,
        )
        return copy.deepcopy(issue)

    def update_issue(self, number: int, changes: IssueChanges) -> Issue:
        self.updates.append((number, changes))
        issue = self.issues[number]
        if changes.title is not None:
            issue.title = changes.title
        if changes.body is not None:
            issue.body = changes.body
        if changes.state is not None:
            issue.state = changes.state
        if changes.labels is not None:
            colors = {label.name.lower(): label.color for label in issue.labels}
            issue.labels = [Label(name, colors.get(name.lower())) for name in changes.labels]
        if changes.assignees is not None:
            issue.assignees = [GitUser(login) for login in changes.assignees]
        if changes.due_date is not None:
            issue.due_date = changes.due_date
        elif changes.clear_due_date:
            issue.due_date = None
        return copy.deepcopy(issue)
