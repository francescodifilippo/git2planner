"""Command line interface."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from typing import Any

from . import __version__
from .auth import DeviceCodeCredential, create_credential
from .config import (
    DEFAULT_CONFIG_FILE,
    DIRECTIONS,
    PROVIDERS,
    AppConfig,
    SyncConfig,
    load_config,
)
from .errors import AuthError, ConfigError, Git2PlannerError, HTTPError, PermissionDeniedError
from .graph import GraphClient
from .mapping import CATEGORY_SLOTS, PLANNER_CATEGORY_COLORS
from .providers import create_provider
from .state import StateStore
from .sync import SyncEngine, SyncReport
from .templates import example_config

log = logging.getLogger("git2planner")

EXIT_OK = 0
EXIT_SYNC_ERRORS = 1
EXIT_CONFIG = 2
EXIT_AUTH = 3


def _setup_logging(verbosity: int, quiet: bool) -> None:
    level = logging.WARNING if quiet else logging.DEBUG if verbosity > 0 else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    root = logging.getLogger("git2planner")
    root.handlers[:] = [handler]
    root.setLevel(level)
    root.propagate = False
    logging.getLogger("urllib3").setLevel(logging.DEBUG if verbosity > 1 else logging.WARNING)


def _config_path(args: argparse.Namespace) -> str:
    return str(args.config or os.environ.get("GIT2PLANNER_CONFIG") or DEFAULT_CONFIG_FILE)


def _load(args: argparse.Namespace) -> AppConfig:
    return load_config(_config_path(args))


def _graph(config: AppConfig) -> GraphClient:
    return GraphClient.from_config(config.graph, interactive=sys.stdin.isatty())


# --------------------------------------------------------------------------- commands


def cmd_sync(args: argparse.Namespace) -> int:
    config = _load(args)
    if args.state:
        config.state_path = Path(args.state)
    if args.no_state:
        config.state_path = None
    syncs = config.select(args.only)
    graph = _graph(config)
    store = StateStore(config.state_path)
    exit_code = EXIT_OK
    with store.locked():
        store.load()
        while True:
            engine = SyncEngine(
                config,
                graph,
                store,
                dry_run=args.dry_run,
                force=args.force,
                reconcile=args.reconcile,
                full=args.full,
                direction=args.direction,
            )
            reports = engine.run([sync.name for sync in syncs], args.issue or None)
            _print_reports(reports, as_json=args.json)
            exit_code = EXIT_OK if all(report.ok for report in reports) else EXIT_SYNC_ERRORS
            if not args.watch:
                break
            log.info("next run in %ds (Ctrl+C to stop)", args.watch)
            try:
                time.sleep(args.watch)
            except KeyboardInterrupt:
                break
    return exit_code


def _print_reports(reports: list[SyncReport], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps([report.to_dict() for report in reports], indent=2))
        return
    for report in reports:
        print(report.summary())
        for error in report.errors:
            print(f"  error: {error}")


class _Checks:
    def __init__(self) -> None:
        self.failed = 0

    def run(self, label: str, check: Callable[[], str | None]) -> bool:
        try:
            detail = check()
        except PermissionDeniedError as exc:
            self.failed += 1
            print(f"  [FAIL] {label}: permission denied ({exc})")
            return False
        except Git2PlannerError as exc:
            self.failed += 1
            print(f"  [FAIL] {label}: {exc}")
            return False
        print(f"  [ OK ] {label}" + (f": {detail}" if detail else ""))
        return True


def cmd_validate(args: argparse.Namespace) -> int:
    config = _load(args)
    path = config.path or _config_path(args)
    print(f"Configuration {path} is valid ({len(config.syncs)} sync(s)).")
    for sync in config.syncs:
        print(
            f"- {sync.name}: {sync.source.provider} {sync.source.repository} -> "
            f"plan {sync.plan_id} ({sync.direction})"
        )
    if args.offline:
        return EXIT_OK

    checks = _Checks()
    print("\nMicrosoft Graph")
    graph = _graph(config)
    if not checks.run(
        f"sign-in ({config.graph.resolved_auth()})",
        lambda: _sign_in(graph),
    ):
        return EXIT_AUTH

    for sync in config.syncs:
        _validate_sync(checks, graph, sync)
    print()
    if checks.failed:
        print(f"{checks.failed} check(s) failed.")
        return EXIT_SYNC_ERRORS
    print("All checks passed.")
    return EXIT_OK


def _validate_sync(checks: _Checks, graph: GraphClient, sync: SyncConfig) -> None:
    print(f"\n{sync.name}")
    provider = create_provider(sync.source)
    checks.run(f"{provider.display_name} repository", provider.check_access)
    plan: dict[str, Any] = {}

    def read_plan() -> str:
        plan.update(graph.get_plan(sync.plan_id))
        return repr(plan.get("title"))

    if not checks.run("Planner plan", read_plan):
        return
    group_id = graph.plan_group_id(plan)
    checks.run("buckets", partial(_bucket_summary, graph, sync.plan_id))
    if sync.categories.mode != "off":
        checks.run("categories", partial(_category_summary, graph, sync.plan_id))
    if group_id and sync.assignees.enabled and sync.assignees.require_group_membership:
        checks.run("group members", partial(_member_summary, graph, group_id))
    if group_id and sync.attachments.mode == "upload":
        checks.run("group document library", partial(_drive_summary, graph, group_id))


def _member_summary(graph: GraphClient, group_id: str) -> str:
    return f"{len(graph.list_group_member_ids(group_id))} member(s)"


def _drive_summary(graph: GraphClient, group_id: str) -> str:
    return str(graph.get_group_drive(group_id).get("webUrl", ""))


def _sign_in(graph: GraphClient) -> None:
    if graph.credential is not None:
        graph.credential.get_token()


def _bucket_summary(graph: GraphClient, plan_id: str) -> str:
    names = [repr(bucket["name"]) for bucket in graph.list_buckets(plan_id)]
    return ", ".join(names) or "none yet"


def _category_summary(graph: GraphClient, plan_id: str) -> str:
    descriptions = graph.get_plan_details(plan_id).get("categoryDescriptions") or {}
    return f"{sum(1 for value in descriptions.values() if value)} of 25 named"


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.path or _config_path(args))
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite)", file=sys.stderr)
        return EXIT_CONFIG
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(example_config(args.provider), encoding="utf-8")
    print(f"Wrote {target}. Edit it, then run: git2planner validate")
    return EXIT_OK


def cmd_login(args: argparse.Namespace) -> int:
    config = _load(args)
    credential = create_credential(config.graph, interactive=True)
    if not isinstance(credential, DeviceCodeCredential):
        print(
            f"'login' is only needed for device_code authentication "
            f"(configured: {config.graph.resolved_auth()})",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    credential.login()
    print(f"Signed in. Token cache: {credential.cache_path}")
    return EXIT_OK


def cmd_logout(args: argparse.Namespace) -> int:
    config = _load(args)
    credential = create_credential(config.graph)
    if isinstance(credential, DeviceCodeCredential) and credential.clear_cache():
        print(f"Removed {credential.cache_path}")
    else:
        print("Nothing to remove.")
    return EXIT_OK


def cmd_plans(args: argparse.Namespace) -> int:
    config = _load(args)
    graph = _graph(config)
    if args.group:
        plans = graph.list_group_plans(args.group)
    else:
        try:
            plans = graph.list_my_plans()
        except HTTPError as exc:
            print(
                f"cannot list your plans ({exc}); with application permissions use --group <id>",
                file=sys.stderr,
            )
            return EXIT_SYNC_ERRORS
    if args.json:
        print(json.dumps(plans, indent=2))
        return EXIT_OK
    if not plans:
        print("No plans found.")
    for plan in plans:
        print(f"{plan['id']}  {plan.get('title', '')}  (group {graph.plan_group_id(plan)})")
    return EXIT_OK


def cmd_describe_plan(args: argparse.Namespace) -> int:
    config = _load(args)
    graph = _graph(config)
    plan = graph.get_plan(args.plan_id)
    buckets = graph.list_buckets(args.plan_id)
    details = graph.get_plan_details(args.plan_id)
    descriptions = details.get("categoryDescriptions") or {}
    if args.json:
        print(json.dumps({"plan": plan, "buckets": buckets, "details": details}, indent=2))
        return EXIT_OK
    print(f"Plan:   {plan.get('title')} ({plan['id']})")
    print(f"Group:  {graph.plan_group_id(plan)}")
    print("Buckets:")
    for bucket in buckets:
        print(f"  {bucket['id']}  {bucket['name']}")
    print("Categories:")
    for slot in CATEGORY_SLOTS:
        color = PLANNER_CATEGORY_COLORS[slot][0]
        print(f"  {slot:<11} {color:<12} {descriptions.get(slot) or '-'}")
    return EXIT_OK


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="git2planner",
        description="Sync GitHub, Gitea, Forgejo and GitLab issues with Microsoft Planner.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-c",
        "--config",
        help=f"configuration file (default: $GIT2PLANNER_CONFIG or {DEFAULT_CONFIG_FILE})",
    )
    parser.add_argument(
        "-v", "--verbose", action="count", default=0, help="more output (-vv: HTTP)"
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="only warnings and errors")
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    sync = commands.add_parser("sync", help="synchronise issues and tasks")
    sync.add_argument(
        "--dry-run", action="store_true", help="show what would change, write nothing"
    )
    sync.add_argument(
        "--direction",
        choices=[*DIRECTIONS, "push", "pull", "bidirectional"],
        help="override the configured direction for this run",
    )
    sync.add_argument(
        "--only", action="append", metavar="NAME", help="run only this sync (repeatable)"
    )
    sync.add_argument(
        "--issue",
        action="append",
        type=int,
        metavar="NUMBER",
        help="sync only this issue (repeatable)",
    )
    sync.add_argument(
        "--full", action="store_true", help="re-read every closed issue, not only recent ones"
    )
    sync.add_argument("--force", action="store_true", help="rewrite every synced field in Planner")
    sync.add_argument(
        "--reconcile",
        action="store_true",
        help="link existing tasks to issues before creating new ones",
    )
    sync.add_argument(
        "--state", metavar="PATH", help="state file to use instead of the configured one"
    )
    sync.add_argument("--no-state", action="store_true", help="keep state in memory only")
    sync.add_argument("--watch", type=int, metavar="SECONDS", help="repeat every SECONDS seconds")
    sync.add_argument("--json", action="store_true", help="print reports as JSON")
    sync.set_defaults(func=cmd_sync)

    validate = commands.add_parser("validate", help="check the configuration and connectivity")
    validate.add_argument("--offline", action="store_true", help="only validate the file")
    validate.set_defaults(func=cmd_validate)

    init = commands.add_parser("init", help="write an annotated configuration file")
    init.add_argument("path", nargs="?", help=f"where to write it (default: {DEFAULT_CONFIG_FILE})")
    init.add_argument("--provider", choices=PROVIDERS, default="github")
    init.add_argument("--force", action="store_true", help="overwrite an existing file")
    init.set_defaults(func=cmd_init)

    login = commands.add_parser("login", help="sign in with a device code (delegated auth)")
    login.set_defaults(func=cmd_login)
    logout = commands.add_parser("logout", help="forget the cached device-code sign-in")
    logout.set_defaults(func=cmd_logout)

    plans = commands.add_parser("plans", help="list Planner plans and their ids")
    plans.add_argument("--group", metavar="GROUP_ID", help="Microsoft 365 group id")
    plans.add_argument("--json", action="store_true")
    plans.set_defaults(func=cmd_plans)

    describe = commands.add_parser("describe-plan", help="show buckets and categories of a plan")
    describe.add_argument("plan_id")
    describe.add_argument("--json", action="store_true")
    describe.set_defaults(func=cmd_describe_plan)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose, args.quiet)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_CONFIG
    try:
        result: int = args.func(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except AuthError as exc:
        print(f"authentication error: {exc}", file=sys.stderr)
        return EXIT_AUTH
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Git2PlannerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_SYNC_ERRORS
    return result
