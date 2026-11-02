from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from git2planner.config import AppConfig, deep_merge, parse_config
from git2planner.state import StateStore
from git2planner.sync import SyncEngine, SyncReport

from .fakes import PLAN_ID, FakePlanner, FakeProvider


def make_config(**sync: Any) -> AppConfig:
    base = {
        "name": "app",
        "source": {"provider": "github", "repository": "acme/app", "token": "secret"},
        "plan_id": PLAN_ID,
        "buckets": {"default": "To do", "closed": "Done"},
    }
    data = {
        "graph": {"access_token": "token"},
        "state": {"enabled": False},
        "syncs": [deep_merge(base, sync)],
    }
    return parse_config(data, environ={})


@pytest.fixture
def planner() -> FakePlanner:
    return FakePlanner()


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def store(tmp_path: Any) -> StateStore:
    return StateStore(tmp_path / "state.json")


Runner = Callable[..., SyncReport]


@pytest.fixture
def run(planner: FakePlanner, provider: FakeProvider, store: StateStore) -> Runner:
    def _run(
        config: AppConfig | None = None,
        *,
        issues: list[int] | None = None,
        state: StateStore | None = None,
        **kwargs: Any,
    ) -> SyncReport:
        engine = SyncEngine(
            config or make_config(),
            planner,  # type: ignore[arg-type]
            state or store,
            provider_factory=lambda _: provider,
            **kwargs,
        )
        (report,) = engine.run(issue_numbers=issues)
        return report

    return _run
