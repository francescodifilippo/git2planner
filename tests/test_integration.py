"""End-to-end run with the real HTTP clients against mocked GitHub and Graph APIs."""

from __future__ import annotations

import json

import responses
from responses import matchers

from git2planner.config import parse_config
from git2planner.graph import GraphClient
from git2planner.http import HttpClient, StaticHeaderAuth
from git2planner.state import StateStore
from git2planner.sync import SyncEngine

GH = "https://api.github.com"
GRAPH = "https://graph.microsoft.com/v1.0"


@responses.activate
def test_github_issue_becomes_planner_task(tmp_path):
    responses.get(
        f"{GH}/repos/acme/app/issues",
        json=[
            {
                "number": 7,
                "title": "Broken build",
                "body": "- [ ] fix CI",
                "state": "open",
                "html_url": "https://github.com/acme/app/issues/7",
                "labels": [{"name": "ci", "color": "0e8a16"}],
                "assignees": [],
            }
        ],
    )
    responses.get(
        f"{GRAPH}/planner/plans/plan-1",
        json={"id": "plan-1", "title": "Eng", "container": {"type": "group", "containerId": "g1"}},
    )
    responses.get(
        f"{GRAPH}/planner/plans/plan-1/details",
        json={"categoryDescriptions": {}, "@odata.etag": "pd1"},
    )
    responses.get(f"{GRAPH}/planner/plans/plan-1/buckets", json={"value": []})
    responses.get(f"{GRAPH}/planner/plans/plan-1/tasks", json={"value": []})
    responses.patch(
        f"{GRAPH}/planner/plans/plan-1/details",
        json={"categoryDescriptions": {"category4": "ci"}, "@odata.etag": "pd2"},
        match=[matchers.header_matcher({"If-Match": "pd1"})],
    )
    responses.post(f"{GRAPH}/planner/buckets", json={"id": "b1", "name": "To do"})
    responses.post(f"{GRAPH}/planner/tasks", json={"id": "t1", "@odata.etag": "te1"})
    responses.get(f"{GRAPH}/planner/tasks/t1/details", json={"id": "t1", "@odata.etag": "de1"})
    responses.patch(
        f"{GRAPH}/planner/tasks/t1/details",
        json={"id": "t1", "@odata.etag": "de2"},
        match=[matchers.header_matcher({"If-Match": "de1"})],
    )

    config = parse_config(
        {
            "graph": {"access_token": "token"},
            "syncs": [
                {
                    "source": {"provider": "github", "repository": "acme/app", "token": "gh"},
                    "plan_id": "plan-1",
                    "assignees": {"enabled": False},
                }
            ],
        },
        environ={},
    )
    graph = GraphClient(
        HttpClient(
            GRAPH, auth=StaticHeaderAuth({"Authorization": "Bearer token"}), sleep=lambda _: None
        ),
        sleep=lambda _: None,
    )
    store = StateStore(tmp_path / "state.json")
    (report,) = SyncEngine(config, graph, store).run()

    assert report.ok, report.errors
    assert report.created == 1
    created = next(c for c in responses.calls if c.request.url == f"{GRAPH}/planner/tasks")
    payload = json.loads(created.request.body)
    assert payload["title"] == "#7 Broken build"
    assert payload["bucketId"] == "b1"
    assert payload["appliedCategories"] == {"category4": True}
    details = json.loads(responses.calls[-1].request.body)
    assert [item["title"] for item in details["checklist"].values()] == ["fix CI"]
    assert "https%3A//github%2Ecom/acme/app/issues/7" in details["references"]

    saved = json.loads((tmp_path / "state.json").read_text())
    record = saved["syncs"]["acme/app"]["issues"]["7"]
    assert record["task_id"] == "t1"
    assert record["details_etag"] == "de2"
