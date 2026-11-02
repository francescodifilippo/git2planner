"""Microsoft Graph client covering the Planner, directory and drive endpoints we need."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import quote

from .auth import TokenCredential, create_credential
from .config import GraphConfig
from .errors import HTTPError, NotFoundError, PreconditionFailedError
from .http import HttpClient

log = logging.getLogger(__name__)

MAX_CONFLICT_RETRIES = 3
BATCH_SIZE = 20
SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024
UPLOAD_CHUNK = 320 * 1024 * 16  # must be a multiple of 320 KiB
USER_FIELDS = "id,displayName,userPrincipalName,mail"


def etag_of(resource: Mapping[str, Any]) -> str | None:
    value = resource.get("@odata.etag")
    return str(value) if value else None


class GraphClient:
    """Typed helpers over the Microsoft Graph REST API."""

    def __init__(
        self,
        http: HttpClient,
        credential: TokenCredential | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.http = http
        self.credential = credential
        self._sleep = sleep

    @classmethod
    def from_config(cls, config: GraphConfig, *, interactive: bool = False) -> GraphClient:
        credential = create_credential(config, interactive=interactive)
        http = HttpClient(
            config.graph_url,
            auth=credential,
            headers={"Accept": "application/json"},
            timeout=config.timeout,
        )
        return cls(http, credential)

    # ------------------------------------------------------------------ versioned writes

    def _write_versioned(
        self,
        method: str,
        path: str,
        patch: Mapping[str, Any] | None,
        etag: str | None,
    ) -> dict[str, Any] | None:
        """PATCH/DELETE with ``If-Match``; refresh the ETag and retry on HTTP 412/409."""
        current = etag
        for attempt in range(MAX_CONFLICT_RETRIES):
            if current is None:
                current = etag_of(self.http.get_json(path))
                if current is None:
                    raise HTTPError(f"{path} has no @odata.etag", url=path)
            try:
                response = self.http.request(
                    method,
                    path,
                    json=patch,
                    headers={"If-Match": current, "Prefer": "return=representation"},
                )
            except PreconditionFailedError:
                if attempt == MAX_CONFLICT_RETRIES - 1:
                    raise
                log.debug("%s %s: ETag is stale, refreshing", method, path)
                current = None
                continue
            if method == "DELETE":
                return None
            if response.status_code == 204 or not response.content:
                result: dict[str, Any] = self.http.get_json(path)
                return result
            data: dict[str, Any] = response.json()
            return data
        raise AssertionError("unreachable")

    # ------------------------------------------------------------------ plans

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        data: dict[str, Any] = self.http.get_json(f"/planner/plans/{plan_id}")
        return data

    @staticmethod
    def plan_group_id(plan: Mapping[str, Any]) -> str | None:
        container = plan.get("container") or {}
        if container.get("type") == "group" and container.get("containerId"):
            return str(container["containerId"])
        owner = plan.get("owner")
        return str(owner) if owner else None

    def list_group_plans(self, group_id: str) -> list[dict[str, Any]]:
        return list(self.http.paginate_odata(f"/groups/{group_id}/planner/plans"))

    def list_my_plans(self) -> list[dict[str, Any]]:
        return list(self.http.paginate_odata("/me/planner/plans"))

    def get_plan_details(self, plan_id: str) -> dict[str, Any]:
        data: dict[str, Any] = self.http.get_json(f"/planner/plans/{plan_id}/details")
        return data

    def update_plan_details(
        self, plan_id: str, patch: Mapping[str, Any], etag: str | None = None
    ) -> dict[str, Any]:
        result = self._write_versioned("PATCH", f"/planner/plans/{plan_id}/details", patch, etag)
        assert result is not None
        return result

    # ------------------------------------------------------------------ buckets

    def list_buckets(self, plan_id: str) -> list[dict[str, Any]]:
        return list(self.http.paginate_odata(f"/planner/plans/{plan_id}/buckets"))

    def create_bucket(self, plan_id: str, name: str) -> dict[str, Any]:
        data: dict[str, Any] = self.http.request(
            "POST",
            "/planner/buckets",
            json={"name": name, "planId": plan_id, "orderHint": " !"},
        ).json()
        return data

    # ------------------------------------------------------------------ tasks

    def list_tasks(self, plan_id: str) -> list[dict[str, Any]]:
        return list(self.http.paginate_odata(f"/planner/plans/{plan_id}/tasks"))

    def get_task(self, task_id: str) -> dict[str, Any]:
        data: dict[str, Any] = self.http.get_json(f"/planner/tasks/{task_id}")
        return data

    def create_task(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        data: dict[str, Any] = self.http.request("POST", "/planner/tasks", json=payload).json()
        return data

    def update_task(
        self, task_id: str, patch: Mapping[str, Any], etag: str | None = None
    ) -> dict[str, Any]:
        result = self._write_versioned("PATCH", f"/planner/tasks/{task_id}", patch, etag)
        assert result is not None
        return result

    def delete_task(self, task_id: str, etag: str | None = None) -> None:
        self._write_versioned("DELETE", f"/planner/tasks/{task_id}", None, etag)

    def get_task_details(self, task_id: str, *, retries: int = 4) -> dict[str, Any]:
        """Fetch task details; freshly created tasks may need a moment to replicate."""
        for attempt in range(retries + 1):
            try:
                data: dict[str, Any] = self.http.get_json(f"/planner/tasks/{task_id}/details")
                return data
            except NotFoundError:
                if attempt == retries:
                    raise
                self._sleep(1.0 + attempt)
        raise AssertionError("unreachable")

    def update_task_details(
        self, task_id: str, patch: Mapping[str, Any], etag: str | None = None
    ) -> dict[str, Any]:
        result = self._write_versioned("PATCH", f"/planner/tasks/{task_id}/details", patch, etag)
        assert result is not None
        return result

    def batch_get(self, paths: Iterable[str]) -> dict[str, tuple[int, dict[str, Any]]]:
        """GET many resources through ``/$batch``; returns ``{path: (status, body)}``."""
        pending = list(dict.fromkeys(paths))
        results: dict[str, tuple[int, dict[str, Any]]] = {}
        for start in range(0, len(pending), BATCH_SIZE):
            chunk = pending[start : start + BATCH_SIZE]
            requests_ = [
                {"id": str(index), "method": "GET", "url": path} for index, path in enumerate(chunk)
            ]
            payload = self.http.request("POST", "/$batch", json={"requests": requests_}).json()
            retry: list[str] = []
            for item in payload.get("responses", []):
                path = chunk[int(item["id"])]
                status = int(item.get("status", 500))
                if status in (429, 503, 504):
                    retry.append(path)
                else:
                    results[path] = (status, item.get("body") or {})
            for path in retry:
                try:
                    results[path] = (200, self.http.get_json(path))
                except NotFoundError:
                    results[path] = (404, {})
        return results

    # ------------------------------------------------------------------ directory

    def get_user(self, identifier: str) -> dict[str, Any] | None:
        try:
            data: dict[str, Any] = self.http.get_json(
                f"/users/{quote(identifier, safe='@')}", params={"$select": USER_FIELDS}
            )
        except NotFoundError:
            return None
        return data

    def find_user_by_email(self, email: str) -> dict[str, Any] | None:
        escaped = email.replace("'", "''")
        payload = self.http.get_json(
            "/users",
            params={
                "$filter": f"mail eq '{escaped}' or userPrincipalName eq '{escaped}'",
                "$select": USER_FIELDS,
            },
        )
        values: list[dict[str, Any]] = payload.get("value", [])
        return values[0] if values else None

    def list_group_member_ids(self, group_id: str) -> set[str]:
        return {
            str(member["id"])
            for member in self.http.paginate_odata(
                f"/groups/{group_id}/members", params={"$select": "id", "$top": 999}
            )
            if member.get("id")
        }

    def get_group(self, group_id: str) -> dict[str, Any]:
        data: dict[str, Any] = self.http.get_json(
            f"/groups/{group_id}", params={"$select": "id,displayName,mail"}
        )
        return data

    # ------------------------------------------------------------------ files

    def get_group_drive(self, group_id: str) -> dict[str, Any]:
        data: dict[str, Any] = self.http.get_json(f"/groups/{group_id}/drive")
        return data

    def upload_group_file(
        self,
        group_id: str,
        path: str,
        content: bytes,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Upload ``content`` to ``path`` in the group's document library (replacing it)."""
        encoded = "/".join(quote(segment, safe="") for segment in path.split("/") if segment)
        item = f"/groups/{group_id}/drive/root:/{encoded}"
        if len(content) <= SIMPLE_UPLOAD_LIMIT:
            data: dict[str, Any] = self.http.request(
                "PUT",
                f"{item}:/content",
                params={"@microsoft.graph.conflictBehavior": "replace"},
                data=content,
                headers={"Content-Type": content_type or "application/octet-stream"},
            ).json()
            return data

        session = self.http.request(
            "POST",
            f"{item}:/createUploadSession",
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
        ).json()
        upload_url = str(session["uploadUrl"])
        total = len(content)
        offset = 0
        while offset < total:
            chunk = content[offset : offset + UPLOAD_CHUNK]
            end = offset + len(chunk) - 1
            response = self.http.request(
                "PUT",
                upload_url,
                data=chunk,
                headers={
                    "Content-Range": f"bytes {offset}-{end}/{total}",
                    "Content-Length": str(len(chunk)),
                },
                authenticated=False,
            )
            offset = end + 1
            if response.status_code in (200, 201):
                result: dict[str, Any] = response.json()
                return result
        raise HTTPError(f"upload of {path} finished without returning the drive item", url=item)
