"""GitLab.com and self-managed GitLab (REST API v4)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlparse

from ..config import BoardConfig
from ..errors import AttachmentError, Git2PlannerError
from ..http import HttpClient, StaticHeaderAuth
from ..models import (
    Attachment,
    GitUser,
    Issue,
    IssueChanges,
    Label,
    Milestone,
    format_datetime,
    parse_datetime,
)
from .base import Board, IssueProvider

PUBLIC_WEB = "https://gitlab.com"
_STATE_PARAM = {"open": "opened", "closed": "closed", "all": "all"}
_RELATIVE_UPLOAD = re.compile(r"""(\]\(\s*<?|\bsrc\s*=\s*["']|\bhref\s*=\s*["'])/uploads/""")
_UPLOAD_PATH = re.compile(r"/uploads/([0-9a-fA-F]{16,64})/([^/?#]+)$")


class GitLabLabelBoard(Board):
    """A GitLab issue board: every list is backed by a label."""

    can_clear = True

    def __init__(self, provider: GitLabProvider, title: str, columns: list[str]) -> None:
        super().__init__(title, columns)
        self.provider = provider
        self._lowered = {column.lower() for column in columns}

    def column_of(self, issue: Issue) -> str | None:
        labels = {name.lower() for name in issue.label_names}
        return next((column for column in self.columns if column.lower() in labels), None)

    def move(self, issue: Issue, column: str | None) -> Issue:
        labels = [name for name in issue.label_names if name.lower() not in self._lowered]
        if column is not None:
            labels.append(column)
        if labels == issue.label_names:
            return issue
        return self.provider.update_issue(issue.number, IssueChanges(labels=labels))


class GitLabProvider(IssueProvider):
    kind = "gitlab"
    display_name = "GitLab"

    def __init__(self, config: Any, http: HttpClient | None = None) -> None:
        base = (config.url or PUBLIC_WEB).rstrip("/")
        if base.endswith("/api/v4"):
            base = base[: -len("/api/v4")]
        self._web_url = base
        self.api_url = f"{base}/api/v4"
        self.project_id = quote(config.repository, safe="")
        self._user_ids: dict[str, int | None] = {}
        super().__init__(config, http)

    @property
    def web_url(self) -> str:
        return self._web_url

    def _create_http(self) -> HttpClient:
        auth = StaticHeaderAuth({"PRIVATE-TOKEN": self.config.token}) if self.config.token else None
        return HttpClient(
            self.api_url,
            auth=auth,
            headers={"Accept": "application/json"},
            verify=self.config.verify_ssl,
            timeout=self.config.timeout,
        )

    def _absolutize(self, body: str) -> str:
        """GitLab stores uploads as project-relative ``/uploads/...`` links."""
        return _RELATIVE_UPLOAD.sub(lambda m: f"{m.group(1)}{self.project_web_url}/uploads/", body)

    def _to_issue(self, data: dict[str, Any]) -> Issue:
        body = self._absolutize(data.get("description") or "")
        milestone = data.get("milestone")
        author = data.get("author")
        labels: list[Label] = []
        for label in data.get("labels") or []:
            if isinstance(label, dict) and label.get("name"):
                labels.append(Label(str(label["name"]), self.label_color(label.get("color"))))
            elif isinstance(label, str):
                labels.append(Label(label))
        number = int(data["iid"])
        return Issue(
            number=number,
            title=str(data.get("title") or ""),
            body=body,
            state="closed" if data.get("state") == "closed" else "open",
            url=str(data.get("web_url") or f"{self.project_web_url}/-/issues/{number}"),
            repository=self.repository,
            labels=labels,
            assignees=[
                GitUser(login=str(a["username"]), name=a.get("name") or None)
                for a in data.get("assignees") or []
                if a and a.get("username")
            ],
            milestone=Milestone(str(milestone["title"]), parse_datetime(milestone.get("due_date")))
            if milestone
            else None,
            due_date=parse_datetime(data.get("due_date")),
            created_at=parse_datetime(data.get("created_at")),
            updated_at=parse_datetime(data.get("updated_at")),
            closed_at=parse_datetime(data.get("closed_at")),
            attachments=self.body_attachments(body),
            author=GitUser(login=str(author["username"]))
            if author and author.get("username")
            else None,
            node_id=str(data["id"]) if data.get("id") is not None else None,
        )

    def iter_issues(
        self,
        state: str = "all",
        since: datetime | None = None,
        *,
        include_pull_requests: bool = False,
    ) -> Iterator[Issue]:
        params: dict[str, Any] = {
            "state": _STATE_PARAM.get(state, "all"),
            "per_page": 100,
            "order_by": "created_at",
            "sort": "asc",
            "with_labels_details": "true",
        }
        if since is not None:
            params["updated_after"] = format_datetime(since)
        for item in self.http.paginate(f"/projects/{self.project_id}/issues", params=params):
            yield self._to_issue(item)

    def get_issue(self, number: int) -> Issue | None:
        response = self.http.request(
            "GET",
            f"/projects/{self.project_id}/issues/{number}",
            params={"with_labels_details": "true"},
            raise_for_status=False,
        )
        if response.status_code in (404, 410):
            return None
        if response.status_code >= 400:
            raise self.http.to_error("GET", response.url, response)
        return self._to_issue(response.json())

    def _fetch_user(self, login: str) -> GitUser | None:
        users = self.http.get_json("/users", params={"username": login})
        if not users:
            return None
        summary = users[0]
        details = self.http.get_json(f"/users/{summary['id']}")
        return GitUser(
            login=login,
            email=details.get("public_email") or details.get("email") or None,
            name=details.get("name") or summary.get("name") or None,
        )

    def check_access(self) -> str:
        data = self.http.get_json(f"/projects/{self.project_id}")
        name = data.get("path_with_namespace", self.repository)
        if data.get("issues_enabled") is False or data.get("issues_access_level") == "disabled":
            return f"{name} (warning: issues are disabled)"
        return f"{name} ({data.get('visibility', 'unknown visibility')})"

    def is_attachment_url(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.hostname == urlparse(self.web_url).hostname and bool(
            _UPLOAD_PATH.search(parsed.path)
        )

    def download(self, attachment: Attachment, max_bytes: int) -> tuple[bytes, str | None]:
        """Prefer the authenticated API route (GitLab 17.2+), fall back to the web URL."""
        match = _UPLOAD_PATH.search(urlparse(attachment.url).path)
        if match:
            api_url = (
                f"{self.api_url}/projects/{self.project_id}/uploads/"
                f"{match.group(1)}/{match.group(2)}"
            )
            try:
                return super().download(
                    Attachment(attachment.name, api_url, attachment.size), max_bytes
                )
            except AttachmentError as exc:
                if "limit" in str(exc):
                    raise
        return super().download(attachment, max_bytes)

    # ------------------------------------------------------------------ boards

    def load_board(self, config: BoardConfig) -> Board:
        boards = list(self.http.paginate(f"/projects/{self.project_id}/boards"))
        wanted = str(config.project).strip().lower() if config.project else None
        board = next(
            (
                item
                for item in boards
                if wanted is None
                or wanted in (str(item.get("id")), str(item.get("name", "")).lower())
            ),
            None,
        )
        if board is None:
            name = f"{config.project!r} " if config.project else ""
            raise Git2PlannerError(f"GitLab issue board {name}not found in {self.repository}")
        lists = sorted(board.get("lists") or [], key=lambda item: int(item.get("position") or 0))
        columns = [
            str(item["label"]["name"])
            for item in lists
            if isinstance(item.get("label"), dict) and item["label"].get("name")
        ]
        return GitLabLabelBoard(self, str(board.get("name") or board.get("id")), columns)

    # ------------------------------------------------------------------ writes

    def _user_id(self, login: str) -> int | None:
        key = login.lower()
        if key not in self._user_ids:
            users = self.http.get_json("/users", params={"username": login})
            self._user_ids[key] = int(users[0]["id"]) if users else None
        return self._user_ids[key]

    def _payload(self, changes: IssueChanges) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if changes.title is not None:
            payload["title"] = changes.title
        if changes.body is not None:
            payload["description"] = changes.body
        if changes.state is not None:
            payload["state_event"] = "close" if changes.state == "closed" else "reopen"
        if changes.labels is not None:
            payload["labels"] = ",".join(changes.labels)
        if changes.assignees is not None:
            ids = [uid for login in changes.assignees if (uid := self._user_id(login)) is not None]
            payload["assignee_ids"] = ids or [0]
        if changes.due_date is not None:
            payload["due_date"] = changes.due_date.date().isoformat()
        elif changes.clear_due_date:
            payload["due_date"] = None
        return payload

    def create_issue(self, changes: IssueChanges) -> Issue:
        if not changes.title:
            raise Git2PlannerError("an issue needs a title")
        payload = self._payload(changes)
        payload.pop("state_event", None)
        if payload.get("assignee_ids") == [0]:
            payload.pop("assignee_ids")
        data = self.http.request("POST", f"/projects/{self.project_id}/issues", json=payload).json()
        issue = self._to_issue(data)
        if changes.state == "closed":
            issue = self.update_issue(issue.number, IssueChanges(state="closed"))
        return issue

    def update_issue(self, number: int, changes: IssueChanges) -> Issue:
        payload = self._payload(changes)
        if not payload:
            issue = self.get_issue(number)
            if issue is None:
                raise Git2PlannerError(f"issue #{number} not found")
            return issue
        data = self.http.request(
            "PUT", f"/projects/{self.project_id}/issues/{number}", json=payload
        ).json()
        return self._to_issue(data)
