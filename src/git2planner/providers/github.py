"""GitHub and GitHub Enterprise Server."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from ..config import BoardConfig
from ..errors import Git2PlannerError, HTTPError
from ..http import HttpClient, StaticHeaderAuth
from ..models import (
    GitUser,
    Issue,
    IssueChanges,
    Label,
    Milestone,
    format_datetime,
    parse_datetime,
)
from .base import Board, IssueProvider

log = logging.getLogger(__name__)

PUBLIC_API = "https://api.github.com"
PUBLIC_WEB = "https://github.com"
_ATTACHMENT_HOSTS = frozenset(
    {"user-images.githubusercontent.com", "private-user-images.githubusercontent.com"}
)
_ATTACHMENT_PATH = re.compile(
    r"^/(?:user-attachments/(?:assets|files)/|[^/]+/[^/]+/(?:files|assets)/\d+/)"
)


_PROJECT_QUERY = """
query($owner: String!, $number: Int!, $field: String!, $cursor: String) {
  repositoryOwner(login: $owner) {
    ... on ProjectV2Owner {
      projectV2(number: $number) {
        id
        title
        field(name: $field) {
          ... on ProjectV2SingleSelectField { id name options { id name } }
        }
        items(first: 100, after: $cursor) {
          pageInfo { hasNextPage endCursor }
          nodes {
            id
            fieldValueByName(name: $field) {
              ... on ProjectV2ItemFieldSingleSelectValue { name }
            }
            content {
              ... on Issue { number repository { nameWithOwner } }
            }
          }
        }
      }
    }
  }
}
"""

_ADD_ITEM = """
mutation($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) { item { id } }
}
"""

_SET_COLUMN = """
mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(
    input: {projectId: $project, itemId: $item, fieldId: $field,
            value: {singleSelectOptionId: $option}}
  ) { projectV2Item { id } }
}
"""


class GitHubProjectBoard(Board):
    """A GitHub Projects board: columns are the options of a single-select field."""

    def __init__(
        self,
        provider: GitHubProvider,
        project_id: str,
        title: str,
        field_id: str,
        options: dict[str, str],
        items: dict[int, tuple[str, str | None]],
    ) -> None:
        super().__init__(title, list(options))
        self.provider = provider
        self.project_id = project_id
        self.field_id = field_id
        self.options = options
        self.items = items

    def column_of(self, issue: Issue) -> str | None:
        entry = self.items.get(issue.number)
        return entry[1] if entry else None

    def move(self, issue: Issue, column: str | None) -> Issue:
        if column is None or column not in self.options:
            return issue
        entry = self.items.get(issue.number)
        if entry is None:
            node_id = issue.node_id
            if node_id is None:
                fresh = self.provider.get_issue(issue.number)
                node_id = fresh.node_id if fresh else None
            if node_id is None:
                raise Git2PlannerError(f"cannot add issue #{issue.number} to the project")
            data = self.provider.graphql(
                _ADD_ITEM, {"project": self.project_id, "content": node_id}
            )
            item_id = str(data["addProjectV2ItemById"]["item"]["id"])
        else:
            item_id = entry[0]
        self.provider.graphql(
            _SET_COLUMN,
            {
                "project": self.project_id,
                "item": item_id,
                "field": self.field_id,
                "option": self.options[column],
            },
        )
        self.items[issue.number] = (item_id, column)
        return issue


class GitHubProvider(IssueProvider):
    kind = "github"
    display_name = "GitHub"
    supports_due_date = False

    def __init__(self, config: Any, http: HttpClient | None = None) -> None:
        url = (config.url or PUBLIC_API).rstrip("/")
        host = urlparse(url).hostname or ""
        if host in ("github.com", "api.github.com", "www.github.com"):
            self.api_url, self._web_url = PUBLIC_API, PUBLIC_WEB
        else:
            base = url[: -len("/api/v3")] if url.endswith("/api/v3") else url
            self.api_url, self._web_url = f"{base}/api/v3", base
        self._assignable: dict[str, bool] = {}
        super().__init__(config, http)

    @property
    def web_url(self) -> str:
        return self._web_url

    def _create_http(self) -> HttpClient:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        auth = (
            StaticHeaderAuth({"Authorization": f"Bearer {self.config.token}"})
            if self.config.token
            else None
        )
        return HttpClient(
            self.api_url,
            auth=auth,
            headers=headers,
            verify=self.config.verify_ssl,
            timeout=self.config.timeout,
        )

    def _to_issue(self, data: dict[str, Any]) -> Issue:
        body = data.get("body") or ""
        milestone = data.get("milestone")
        assignees = data.get("assignees") or ([data["assignee"]] if data.get("assignee") else [])
        user = data.get("user")
        return Issue(
            number=int(data["number"]),
            title=str(data.get("title") or ""),
            body=body,
            state="closed" if data.get("state") == "closed" else "open",
            url=str(data.get("html_url") or f"{self.project_web_url}/issues/{data['number']}"),
            repository=self.repository,
            labels=[
                Label(str(label["name"]), self.label_color(label.get("color")))
                for label in data.get("labels") or []
                if isinstance(label, dict) and label.get("name")
            ],
            assignees=[GitUser(login=str(a["login"])) for a in assignees if a and a.get("login")],
            milestone=Milestone(str(milestone["title"]), parse_datetime(milestone.get("due_on")))
            if milestone
            else None,
            created_at=parse_datetime(data.get("created_at")),
            updated_at=parse_datetime(data.get("updated_at")),
            closed_at=parse_datetime(data.get("closed_at")),
            attachments=self.body_attachments(body),
            author=GitUser(login=str(user["login"])) if user and user.get("login") else None,
            is_pull_request="pull_request" in data,
            state_reason=data.get("state_reason"),
            node_id=str(data["node_id"]) if data.get("node_id") else None,
        )

    def iter_issues(
        self,
        state: str = "all",
        since: datetime | None = None,
        *,
        include_pull_requests: bool = False,
    ) -> Iterator[Issue]:
        params: dict[str, Any] = {
            "state": state,
            "per_page": 100,
            "sort": "created",
            "direction": "asc",
        }
        if since is not None:
            params["since"] = format_datetime(since)
        for item in self.http.paginate(f"/repos/{self.repository}/issues", params=params):
            if "pull_request" in item and not include_pull_requests:
                continue
            yield self._to_issue(item)

    def get_issue(self, number: int) -> Issue | None:
        response = self.http.request(
            "GET", f"/repos/{self.repository}/issues/{number}", raise_for_status=False
        )
        if response.status_code in (404, 410):
            return None
        if response.status_code >= 400:
            raise self.http.to_error("GET", response.url, response)
        data = response.json()
        repository_url = str(data.get("repository_url") or "")
        if repository_url and not repository_url.lower().endswith(
            f"/repos/{self.repository.lower()}"
        ):
            return None
        return self._to_issue(data)

    def _fetch_user(self, login: str) -> GitUser | None:
        data = self.http.get_json(f"/users/{login}")
        return GitUser(login=login, email=data.get("email") or None, name=data.get("name") or None)

    def check_access(self) -> str:
        data = self.http.get_json(f"/repos/{self.repository}")
        if not data.get("has_issues", True):
            return f"{data.get('full_name', self.repository)} (warning: issues are disabled)"
        visibility = "private" if data.get("private") else "public"
        return f"{data.get('full_name', self.repository)} ({visibility})"

    def is_attachment_url(self, url: str) -> bool:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if host in _ATTACHMENT_HOSTS:
            return True
        return host == urlparse(self.web_url).hostname and bool(_ATTACHMENT_PATH.match(parsed.path))

    # ------------------------------------------------------------------ projects

    @property
    def graphql_url(self) -> str:
        if self.api_url == PUBLIC_API:
            return f"{PUBLIC_API}/graphql"
        return f"{self._web_url}/api/graphql"

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload = self.http.request(
            "POST", self.graphql_url, json={"query": query, "variables": variables}
        ).json()
        errors = payload.get("errors")
        if errors:
            messages = "; ".join(str(error.get("message", error)) for error in errors)
            raise HTTPError(f"GitHub GraphQL: {messages}", url=self.graphql_url)
        data: dict[str, Any] = payload.get("data") or {}
        return data

    def load_board(self, config: BoardConfig) -> Board:
        number = int(str(config.project))
        owner = config.owner or self.config.owner
        variables: dict[str, Any] = {"owner": owner, "number": number, "field": config.field}
        items: dict[int, tuple[str, str | None]] = {}
        project: dict[str, Any] = {}
        while True:
            data = self.graphql(_PROJECT_QUERY, variables)
            project = (data.get("repositoryOwner") or {}).get("projectV2") or {}
            if not project:
                raise Git2PlannerError(
                    f"GitHub project {owner}/{number} not found or not readable "
                    "(the token needs the 'project' or 'read:project' scope)"
                )
            field = project.get("field") or {}
            if "options" not in field:
                raise Git2PlannerError(
                    f"field {config.field!r} of GitHub project {owner}/{number} "
                    "is missing or not a single-select field"
                )
            for node in project["items"]["nodes"]:
                content = (node or {}).get("content") or {}
                repository = (content.get("repository") or {}).get("nameWithOwner", "")
                if content.get("number") is None or repository.lower() != self.repository.lower():
                    continue
                value = node.get("fieldValueByName") or {}
                items[int(content["number"])] = (str(node["id"]), value.get("name"))
            page = project["items"]["pageInfo"]
            if not page.get("hasNextPage"):
                break
            variables["cursor"] = page["endCursor"]
        field = project["field"]
        options = {str(option["name"]): str(option["id"]) for option in field["options"]}
        return GitHubProjectBoard(
            self,
            str(project["id"]),
            str(project.get("title") or number),
            str(field["id"]),
            options,
            items,
        )

    # ------------------------------------------------------------------ writes

    def _can_assign(self, login: str) -> bool:
        key = login.lower()
        if key not in self._assignable:
            response = self.http.request(
                "GET", f"/repos/{self.repository}/assignees/{login}", raise_for_status=False
            )
            self._assignable[key] = response.status_code == 204
            if response.status_code != 204:
                log.warning(
                    "%s user %s cannot be assigned in %s", self.display_name, login, self.repository
                )
        return self._assignable[key]

    def _payload(self, changes: IssueChanges) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if changes.title is not None:
            payload["title"] = changes.title
        if changes.body is not None:
            payload["body"] = changes.body
        if changes.state is not None:
            payload["state"] = changes.state
            payload["state_reason"] = "completed" if changes.state == "closed" else "reopened"
        if changes.labels is not None:
            payload["labels"] = list(changes.labels)
        if changes.assignees is not None:
            payload["assignees"] = [login for login in changes.assignees if self._can_assign(login)]
        return payload

    def create_issue(self, changes: IssueChanges) -> Issue:
        if not changes.title:
            raise Git2PlannerError("an issue needs a title")
        payload = self._payload(
            IssueChanges(
                title=changes.title,
                body=changes.body or "",
                labels=changes.labels,
                assignees=changes.assignees,
            )
        )
        data = self.http.request("POST", f"/repos/{self.repository}/issues", json=payload).json()
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
            "PATCH", f"/repos/{self.repository}/issues/{number}", json=payload
        ).json()
        return self._to_issue(data)
