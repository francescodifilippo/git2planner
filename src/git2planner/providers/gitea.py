"""Gitea and Forgejo (including Codeberg)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from ..errors import Git2PlannerError
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
from .base import IssueProvider

PAGE_SIZE = 50
_ATTACHMENT_PATH = re.compile(r"/attachments/[0-9a-fA-F-]{32,36}/?$")


class GiteaProvider(IssueProvider):
    kind = "gitea"
    display_name = "Gitea"

    def __init__(self, config: Any, http: HttpClient | None = None) -> None:
        base = (config.url or "").rstrip("/")
        if base.endswith("/api/v1"):
            base = base[: -len("/api/v1")]
        self._web_url = base
        self.api_url = f"{base}/api/v1"
        self._labels: dict[str, int] | None = None
        super().__init__(config, http)

    @property
    def web_url(self) -> str:
        return self._web_url

    def _create_http(self) -> HttpClient:
        auth = (
            StaticHeaderAuth({"Authorization": f"token {self.config.token}"})
            if self.config.token
            else None
        )
        return HttpClient(
            self.api_url,
            auth=auth,
            headers={"Accept": "application/json"},
            verify=self.config.verify_ssl,
            timeout=self.config.timeout,
        )

    def _to_issue(self, data: dict[str, Any]) -> Issue:
        body = data.get("body") or ""
        milestone = data.get("milestone")
        assignees = data.get("assignees") or ([data["assignee"]] if data.get("assignee") else [])
        user = data.get("user")
        native = [
            Attachment(
                name=str(asset.get("name") or "attachment"),
                url=str(asset["browser_download_url"]),
                size=asset.get("size"),
            )
            for asset in data.get("assets") or []
            if asset.get("browser_download_url")
        ]
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
            assignees=[
                GitUser(
                    login=str(a["login"]),
                    email=a.get("email") or None,
                    name=a.get("full_name") or None,
                )
                for a in assignees
                if a and a.get("login")
            ],
            milestone=Milestone(str(milestone["title"]), parse_datetime(milestone.get("due_on")))
            if milestone
            else None,
            due_date=parse_datetime(data.get("due_date")),
            created_at=parse_datetime(data.get("created_at")),
            updated_at=parse_datetime(data.get("updated_at")),
            closed_at=parse_datetime(data.get("closed_at")),
            attachments=self.merge_attachments(native, self.body_attachments(body)),
            author=GitUser(login=str(user["login"])) if user and user.get("login") else None,
            is_pull_request=bool(data.get("pull_request")),
        )

    def iter_issues(
        self,
        state: str = "all",
        since: datetime | None = None,
        *,
        include_pull_requests: bool = False,
    ) -> Iterator[Issue]:
        params: dict[str, Any] = {"state": state, "limit": PAGE_SIZE, "page": 1}
        if not include_pull_requests:
            params["type"] = "issues"
        if since is not None:
            params["since"] = format_datetime(since)
        path = f"/repos/{self.repository}/issues"
        while True:
            response = self.http.request("GET", path, params=params)
            items = response.json() or []
            for item in items:
                if item.get("pull_request") and not include_pull_requests:
                    continue
                yield self._to_issue(item)
            if not items or ("next" not in response.links and len(items) < PAGE_SIZE):
                return
            params["page"] += 1

    def get_issue(self, number: int) -> Issue | None:
        response = self.http.request(
            "GET", f"/repos/{self.repository}/issues/{number}", raise_for_status=False
        )
        if response.status_code in (404, 410):
            return None
        if response.status_code >= 400:
            raise self.http.to_error("GET", response.url, response)
        return self._to_issue(response.json())

    def _fetch_user(self, login: str) -> GitUser | None:
        data = self.http.get_json(f"/users/{login}")
        return GitUser(
            login=login, email=data.get("email") or None, name=data.get("full_name") or None
        )

    def check_access(self) -> str:
        data = self.http.get_json(f"/repos/{self.repository}")
        name = data.get("full_name", self.repository)
        if data.get("has_issues") is False:
            return f"{name} (warning: issues are disabled)"
        visibility = "private" if data.get("private") else "public"
        return f"{name} ({visibility})"

    def is_attachment_url(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.hostname == urlparse(self.web_url).hostname and bool(
            _ATTACHMENT_PATH.search(parsed.path)
        )

    # ------------------------------------------------------------------ writes

    def _load_labels(self) -> dict[str, int]:
        if self._labels is None:
            labels: dict[str, int] = {}
            sources = [f"/repos/{self.repository}/labels", f"/orgs/{self.config.owner}/labels"]
            for path in sources:
                page = 1
                while True:
                    response = self.http.request(
                        "GET",
                        path,
                        params={"limit": PAGE_SIZE, "page": page},
                        raise_for_status=False,
                    )
                    if response.status_code >= 400:
                        break
                    items = response.json() or []
                    for item in items:
                        labels.setdefault(str(item["name"]).lower(), int(item["id"]))
                    if len(items) < PAGE_SIZE:
                        break
                    page += 1
            self._labels = labels
        return self._labels

    def _label_ids(self, names: list[str], colors: dict[str, str]) -> list[int]:
        labels = self._load_labels()
        ids: list[int] = []
        for name in names:
            label_id = labels.get(name.lower())
            if label_id is None:
                color = colors.get(name) or "6c757d"
                created = self.http.request(
                    "POST",
                    f"/repos/{self.repository}/labels",
                    json={"name": name, "color": f"#{color.lstrip('#')}"},
                ).json()
                label_id = int(created["id"])
                labels[name.lower()] = label_id
            if label_id not in ids:
                ids.append(label_id)
        return ids

    def _payload(self, changes: IssueChanges) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if changes.title is not None:
            payload["title"] = changes.title
        if changes.body is not None:
            payload["body"] = changes.body
        if changes.state is not None:
            payload["state"] = changes.state
        if changes.assignees is not None:
            payload["assignees"] = list(changes.assignees)
        if changes.due_date is not None:
            payload["due_date"] = format_datetime(changes.due_date)
        elif changes.clear_due_date:
            payload["unset_due_date"] = True
        return payload

    def create_issue(self, changes: IssueChanges) -> Issue:
        if not changes.title:
            raise Git2PlannerError("an issue needs a title")
        payload = self._payload(changes)
        payload.pop("state", None)
        payload.pop("unset_due_date", None)
        payload.setdefault("body", "")
        if changes.labels:
            payload["labels"] = self._label_ids(changes.labels, changes.label_colors)
        if changes.state == "closed":
            payload["closed"] = True
        data = self.http.request("POST", f"/repos/{self.repository}/issues", json=payload).json()
        return self._to_issue(data)

    def update_issue(self, number: int, changes: IssueChanges) -> Issue:
        if changes.labels is not None:
            self.http.request(
                "PUT",
                f"/repos/{self.repository}/issues/{number}/labels",
                json={"labels": self._label_ids(changes.labels, changes.label_colors)},
            )
        payload = self._payload(changes)
        if payload:
            data = self.http.request(
                "PATCH", f"/repos/{self.repository}/issues/{number}", json=payload
            ).json()
            if changes.labels is None:
                return self._to_issue(data)
        issue = self.get_issue(number)
        if issue is None:
            raise Git2PlannerError(f"issue #{number} not found")
        return issue


class ForgejoProvider(GiteaProvider):
    """Forgejo exposes the Gitea-compatible API under ``/api/v1``."""

    kind = "forgejo"
    display_name = "Forgejo"
