from __future__ import annotations

import json
import os
from urllib.parse import parse_qs

import pytest
import responses
from responses import matchers

from git2planner.auth import (
    AccessTokenCredential,
    ClientAssertionCredential,
    ClientSecretCredential,
    DeviceCodeCredential,
    github_oidc_assertion,
)
from git2planner.errors import (
    AuthError,
    HTTPError,
    NotFoundError,
    PermissionDeniedError,
    PreconditionFailedError,
)
from git2planner.graph import GraphClient
from git2planner.http import HttpClient, StaticHeaderAuth

API = "https://api.test"
GRAPH = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/tenant/oauth2/v2.0/token"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def client(**kwargs) -> tuple[HttpClient, Clock]:
    clock = Clock()
    return HttpClient(API, sleep=clock.sleep, clock=clock, backoff=0.01, **kwargs), clock


# --------------------------------------------------------------------------- http


@responses.activate
def test_retry_after_is_honoured():
    responses.get(f"{API}/x", status=429, headers={"Retry-After": "7"})
    responses.get(f"{API}/x", json={"ok": True})
    http, clock = client()
    assert http.get_json("/x") == {"ok": True}
    assert clock.sleeps == [7.0]


@responses.activate
def test_github_rate_limit_reset_header():
    http, clock = client()
    reset = str(int(clock.now) + 30)
    responses.get(
        f"{API}/x", status=403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset}
    )
    responses.get(f"{API}/x", json=[])
    assert http.get_json("/x") == []
    assert clock.sleeps == [31.0]


@responses.activate
def test_post_is_not_retried_on_bad_gateway():
    responses.post(f"{API}/x", status=502, json={"message": "boom"})
    http, _ = client()
    with pytest.raises(HTTPError, match="boom"):
        http.request("POST", "/x", json={})
    assert len(responses.calls) == 1


@responses.activate
def test_unauthorized_invalidates_credentials_once():
    class Refreshing:
        def __init__(self) -> None:
            self.token = "old"

        def headers(self) -> dict[str, str]:
            return {"Authorization": f"Bearer {self.token}"}

        def invalidate(self) -> None:
            self.token = "new"

    responses.get(
        f"{API}/x", status=401, match=[matchers.header_matcher({"Authorization": "Bearer old"})]
    )
    responses.get(
        f"{API}/x", json={}, match=[matchers.header_matcher({"Authorization": "Bearer new"})]
    )
    http, _ = client(auth=Refreshing())
    assert http.get_json("/x") == {}


@responses.activate
@pytest.mark.parametrize(
    ("status", "error"),
    [
        (404, NotFoundError),
        (403, PermissionDeniedError),
        (412, PreconditionFailedError),
        (400, HTTPError),
    ],
)
def test_error_mapping(status, error):
    responses.get(f"{API}/x", status=status, json={"error": {"code": "E", "message": "nope"}})
    http, _ = client(max_retries=0)
    with pytest.raises(error, match="nope"):
        http.get_json("/x")


@responses.activate
def test_link_and_odata_pagination():
    responses.get(
        f"{API}/items", json=[1, 2], headers={"Link": f'<{API}/items?page=2>; rel="next"'}
    )
    responses.get(f"{API}/items?page=2", json=[3])
    responses.get(f"{API}/odata", json={"value": [1], "@odata.nextLink": f"{API}/odata2"})
    responses.get(f"{API}/odata2", json={"value": [2]})
    http, _ = client(auth=StaticHeaderAuth({"Authorization": "token x"}))
    assert list(http.paginate("/items", params={"per_page": 2})) == [1, 2, 3]
    assert list(http.paginate_odata("/odata")) == [1, 2]


# --------------------------------------------------------------------------- auth


@responses.activate
def test_client_secret_token_is_cached():
    responses.post(TOKEN_URL, json={"access_token": "abc", "expires_in": 3600})
    clock = Clock()
    credential = ClientSecretCredential("tenant", "client", "secret", clock=clock)
    assert credential.get_token() == "abc"
    assert credential.get_token() == "abc"
    assert len(responses.calls) == 1
    body = parse_qs(responses.calls[0].request.body)
    assert body["grant_type"] == ["client_credentials"]
    assert body["scope"] == ["https://graph.microsoft.com/.default"]
    clock.now += 3600
    credential.get_token()
    assert len(responses.calls) == 2


@responses.activate
def test_token_errors_are_readable():
    responses.post(
        TOKEN_URL,
        status=400,
        json={
            "error": "invalid_client",
            "error_description": "AADSTS7000215: Invalid secret.\r\nTrace",
        },
    )
    with pytest.raises(AuthError, match=r"invalid_client: AADSTS7000215: Invalid secret\."):
        ClientSecretCredential("tenant", "client", "bad").get_token()


@responses.activate
def test_client_assertion_and_github_oidc():
    responses.get(
        "https://actions.test/token",
        json={"value": "jwt"},
        match=[matchers.query_param_matcher({"audience": "api://AzureADTokenExchange", "x": "1"})],
    )
    responses.post(TOKEN_URL, json={"access_token": "abc", "expires_in": 60})
    assertion = github_oidc_assertion(
        environ={
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://actions.test/token?x=1",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "t",
        }
    )
    credential = ClientAssertionCredential("tenant", "client", assertion)
    assert credential.get_token() == "abc"
    body = parse_qs(responses.calls[1].request.body)
    assert body["client_assertion"] == ["jwt"]


def test_github_oidc_outside_actions():
    with pytest.raises(AuthError, match="id-token"):
        github_oidc_assertion(environ={})()


@responses.activate
def test_device_code_flow_and_refresh(tmp_path):
    base = "https://login.microsoftonline.com/organizations/oauth2/v2.0"
    responses.post(
        f"{base}/devicecode",
        json={
            "device_code": "dc",
            "message": "Go to https://microsoft.com/devicelogin",
            "interval": 1,
            "expires_in": 60,
        },
    )
    responses.post(f"{base}/token", status=400, json={"error": "authorization_pending"})
    responses.post(
        f"{base}/token", json={"access_token": "a1", "refresh_token": "r1", "expires_in": 3600}
    )
    messages: list[str] = []
    clock = Clock()
    cache = tmp_path / "token.json"
    credential = DeviceCodeCredential(
        "organizations",
        "client",
        ["Tasks.ReadWrite", "offline_access"],
        cache,
        interactive=True,
        prompt=messages.append,
        sleep=clock.sleep,
        clock=clock,
    )
    assert credential.get_token() == "a1"
    assert messages == ["Go to https://microsoft.com/devicelogin"]
    assert json.loads(cache.read_text())["refresh_token"] == "r1"
    if os.name == "posix":
        assert oct(cache.stat().st_mode & 0o777) == "0o600"

    responses.post(
        f"{base}/token", json={"access_token": "a2", "refresh_token": "r2", "expires_in": 3600}
    )
    again = DeviceCodeCredential("organizations", "client", ["Tasks.ReadWrite"], cache, clock=clock)
    assert again.get_token() == "a2"
    assert "refresh_token=r1" in responses.calls[-1].request.body
    assert again.clear_cache()
    with pytest.raises(AuthError, match="git2planner login"):
        DeviceCodeCredential("organizations", "client", [], cache).get_token()


def test_static_access_token():
    assert AccessTokenCredential("t").headers() == {"Authorization": "Bearer t"}


# --------------------------------------------------------------------------- graph


def graph() -> GraphClient:
    http = HttpClient(
        GRAPH, auth=StaticHeaderAuth({"Authorization": "Bearer t"}), sleep=lambda _: None
    )
    return GraphClient(http, sleep=lambda _: None)


@responses.activate
def test_update_task_retries_with_fresh_etag():
    url = f"{GRAPH}/planner/tasks/t1"
    responses.patch(
        url,
        status=412,
        json={"error": {"message": "stale"}},
        match=[matchers.header_matcher({"If-Match": "old"})],
    )
    responses.get(url, json={"id": "t1", "@odata.etag": "fresh"})
    responses.patch(
        url,
        json={"id": "t1", "title": "x", "@odata.etag": "newer"},
        match=[matchers.header_matcher({"If-Match": "fresh", "Prefer": "return=representation"})],
    )
    result = graph().update_task("t1", {"title": "x"}, "old")
    assert result["@odata.etag"] == "newer"


@responses.activate
def test_update_without_representation_refetches():
    url = f"{GRAPH}/planner/tasks/t1/details"
    responses.get(url, json={"id": "t1", "@odata.etag": "e1"})
    responses.patch(url, status=204)
    responses.get(url, json={"id": "t1", "@odata.etag": "e2"})
    assert graph().update_task_details("t1", {"description": "x"})["@odata.etag"] == "e2"


@responses.activate
def test_delete_task_and_plan_group():
    responses.delete(
        f"{GRAPH}/planner/tasks/t1", status=204, match=[matchers.header_matcher({"If-Match": "e"})]
    )
    graph().delete_task("t1", "e")
    assert GraphClient.plan_group_id({"container": {"type": "group", "containerId": "g"}}) == "g"
    assert GraphClient.plan_group_id({"owner": "legacy"}) == "legacy"


@responses.activate
def test_batch_get_retries_throttled_items():
    responses.post(
        f"{GRAPH}/$batch",
        json={
            "responses": [
                {"id": "0", "status": 200, "body": {"id": "a"}},
                {"id": "1", "status": 429, "body": {}},
                {"id": "2", "status": 404, "body": {}},
            ]
        },
    )
    responses.get(f"{GRAPH}/planner/tasks/b/details", json={"id": "b"})
    result = graph().batch_get(
        ["/planner/tasks/a/details", "/planner/tasks/b/details", "/planner/tasks/c/details"]
    )
    assert result["/planner/tasks/a/details"] == (200, {"id": "a"})
    assert result["/planner/tasks/b/details"] == (200, {"id": "b"})
    assert result["/planner/tasks/c/details"][0] == 404


@responses.activate
def test_users_and_members():
    responses.get(f"{GRAPH}/users/alice@contoso.com", status=404, json={"error": {"message": "nf"}})
    responses.get(f"{GRAPH}/users", json={"value": [{"id": "u1"}]})
    responses.get(f"{GRAPH}/groups/g/members", json={"value": [{"id": "u1"}, {"id": "u2"}]})
    client_ = graph()
    assert client_.get_user("alice@contoso.com") is None
    assert client_.find_user_by_email("o'neil@contoso.com") == {"id": "u1"}
    assert "o''neil" in responses.calls[1].request.url.replace("%27", "'")
    assert client_.list_group_member_ids("g") == {"u1", "u2"}


@responses.activate
def test_small_and_large_uploads():
    responses.put(
        f"{GRAPH}/groups/g/drive/root:/git2planner/a%20b.png:/content",
        json={"webUrl": "https://sp/a b.png"},
    )
    assert (
        graph().upload_group_file("g", "git2planner/a b.png", b"x")["webUrl"]
        == "https://sp/a b.png"
    )

    responses.post(
        f"{GRAPH}/groups/g/drive/root:/big.bin:/createUploadSession",
        json={"uploadUrl": "https://upload.test/session"},
    )
    responses.put("https://upload.test/session", status=202, json={})
    responses.put("https://upload.test/session", status=201, json={"webUrl": "https://sp/big.bin"})
    content = b"0" * (5 * 1024 * 1024 + 10)
    assert graph().upload_group_file("g", "big.bin", content)["webUrl"] == "https://sp/big.bin"
    chunk_calls = [
        call for call in responses.calls if call.request.url == "https://upload.test/session"
    ]
    assert chunk_calls[0].request.headers["Content-Range"] == f"bytes 0-5242879/{len(content)}"
    assert "Authorization" not in chunk_calls[0].request.headers
