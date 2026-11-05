"""The synchronisation engine.

Every pair *issue ⇄ task* has a baseline in the state file: the values that
were last written on either side, expressed in Planner terms. A run compares
both sides with that baseline field by field:

* a field that changed only in Git is written to Planner (``git-to-planner``);
* a field that changed only in Planner is written to Git (``planner-to-git``);
* a field that changed on both sides is resolved by the ``conflicts`` policy.

Values that git2planner wrote itself are therefore never mistaken for user
edits, so nothing bounces back distorted.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import logging
import posixpath
import re
import string
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .attachments import (
    decode_reference_key,
    reference_key,
    reference_type,
    sanitize_filename,
    sanitize_folder,
)
from .config import AppConfig, FilterConfig, SyncConfig, normalize_direction
from .errors import AttachmentError, Git2PlannerError, HTTPError, NotFoundError
from .graph import GraphClient, etag_of
from .mapping import (
    AssigneeResolver,
    CategoryMapper,
    bucket_label,
    bucket_name_for,
    column_for_bucket,
    priority_band,
    priority_for,
    priority_label,
)
from .markdown import (
    append_task_items,
    extract_task_items,
    set_task_item_states,
    task_item_lines,
    to_plain_text,
)
from .models import Attachment, Issue, IssueChanges, format_datetime, parse_datetime
from .providers import IssueProvider, create_provider
from .providers.base import Board
from .state import StateStore, SyncState, TaskRecord

log = logging.getLogger(__name__)

MAX_TITLE = 255
MAX_DESCRIPTION = 30000
MAX_CHECKLIST_ITEMS = 20
MAX_CHECKLIST_TITLE = 100
MAX_REFERENCES = 50
MAX_ALIAS = 100
MAX_ASSIGNEES = 20
SINCE_MARGIN = timedelta(minutes=10)
SAVE_EVERY = 20

ASSIGNMENT = {"@odata.type": "#microsoft.graph.plannerAssignment", "orderHint": " !"}
REFERENCE_TYPE = "microsoft.graph.plannerExternalReference"
CHECKLIST_TYPE = "microsoft.graph.plannerChecklistItem"
FOOTER_PREFIX = "Synced from "
_URL = re.compile(r"https?://[^\s<>\"']+")
_FOOTER = re.compile(r"\s*(?:^|\n)---\n" + re.escape(FOOTER_PREFIX) + r"[^\n]*\s*$")


# --------------------------------------------------------------------------- reports


@dataclass
class SyncReport:
    name: str
    dry_run: bool = False
    direction: str = "git-to-planner"
    created: int = 0
    linked: int = 0
    updated: int = 0
    completed: int = 0
    reopened: int = 0
    deleted: int = 0
    unchanged: int = 0
    skipped: int = 0
    pulled: int = 0
    issues_created: int = 0
    conflicts: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        prefix = "[dry-run] " if self.dry_run else ""
        parts: list[str] = []
        if self.direction != "planner-to-git":
            parts += [
                f"created {self.created}",
                f"linked {self.linked}",
                f"updated {self.updated} ({self.completed} completed, {self.reopened} reopened)",
                f"deleted {self.deleted}",
                f"unchanged {self.unchanged}",
                f"skipped {self.skipped}",
            ]
        if self.direction != "git-to-planner":
            parts += [
                f"issues updated from Planner {self.pulled}",
                f"issues created from Planner {self.issues_created}",
                f"conflicts {self.conflicts}",
            ]
        parts += [f"warnings {len(self.warnings)}", f"errors {len(self.errors)}"]
        return f"{prefix}{self.name}: " + ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class _ReportHandler(logging.Handler):
    """Copies warnings logged during a sync into its report."""

    def __init__(self, report: SyncReport) -> None:
        super().__init__(logging.WARNING)
        self.report = report

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno == logging.WARNING:
            self.report.warnings.append(record.getMessage())


# --------------------------------------------------------------------------- helpers


@dataclass
class DesiredTask:
    """A task as it should look according to its issue."""

    title: str
    bucket_id: str
    percent_complete: int
    priority: int
    due: str | None
    assignees: list[str]
    categories: list[str]
    description: str | None
    preview: str
    references: dict[str, dict[str, Any]]
    checklist: dict[str, tuple[str, bool]]

    @property
    def description_hash(self) -> str | None:
        return text_hash(self.description) if self.description is not None else None

    @property
    def checklist_states(self) -> dict[str, bool]:
        return {item_id: checked for item_id, (_, checked) in self.checklist.items()}


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def matches_filters(issue: Issue, filters: FilterConfig) -> bool:
    """Whether a not-yet-tracked issue should get a Planner task."""
    if issue.is_pull_request and not filters.include_pull_requests:
        return False
    if filters.state not in ("all", issue.state):
        return False
    labels = {name.lower() for name in issue.label_names}
    if any(label.lower() not in labels for label in filters.labels):
        return False
    if any(label.lower() in labels for label in filters.exclude_labels):
        return False
    if filters.milestones:
        wanted = {name.lower() for name in filters.milestones}
        if issue.milestone is None or issue.milestone.title.lower() not in wanted:
            return False
    return not (
        filters.created_after is not None
        and issue.created_at is not None
        and issue.created_at < filters.created_after
    )


def _template_values(issue: Issue, provider: IssueProvider) -> dict[str, Any]:
    owner, _, repo = issue.repository.rpartition("/")
    return {
        "number": issue.number,
        "title": issue.title,
        "repository": issue.repository,
        "owner": owner,
        "repo": repo,
        "provider": provider.display_name,
        "state": issue.state,
    }


def render_title(template: str, issue: Issue, provider: IssueProvider) -> str:
    title = template.format(**_template_values(issue, provider)).strip()
    return (title or f"#{issue.number}")[:MAX_TITLE]


def extract_title(template: str, task_title: str, issue: Issue, provider: IssueProvider) -> str:
    """Recover the issue title from a task title rendered with ``template``.

    ``"#12 Fix login"`` with the template ``"#{number} {title}"`` gives
    ``"Fix login"``. Titles that no longer match the template are used as-is.
    """
    values = _template_values(issue, provider)
    pattern: list[str] = []
    has_title = False
    for literal, name, _, _ in string.Formatter().parse(template):
        pattern.append(re.escape(literal).replace(r"\ ", r"\s*"))
        if name is None:
            continue
        if name == "title" and not has_title:
            pattern.append(r"(?P<title>.+?)")
            has_title = True
        elif name == "title":
            pattern.append(r".+?")
        else:
            pattern.append(re.escape(str(values.get(name, ""))))
    text = task_title.strip()
    match = re.fullmatch("".join(pattern), text, flags=re.DOTALL) if has_title else None
    if match and match.group("title").strip():
        return match.group("title").strip()
    return text


def strip_footer(description: str) -> str:
    return _FOOTER.sub("", description or "").rstrip()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _checklist_id(issue: Issue, title: str, occurrence: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"{issue.url}#checklist/{occurrence}/{title}"))


def checklist_items(issue: Issue) -> list[tuple[str, str, bool, int]]:
    """``(id, title, checked, line)`` for the issue's task-list items, as synced."""
    occurrences: Counter[str] = Counter()
    items: list[tuple[str, str, bool, int]] = []
    for item in extract_task_items(issue.body)[:MAX_CHECKLIST_ITEMS]:
        title = _truncate(item.title, MAX_CHECKLIST_TITLE)
        occurrences[title] += 1
        items.append(
            (_checklist_id(issue, title, occurrences[title]), title, item.checked, item.line)
        )
    return items


def due_value(value: datetime | None) -> str | None:
    """Graph value for a due date; date-only values are pinned to 12:00 UTC.

    Noon keeps the calendar day stable for every time zone between UTC-11
    and UTC+11, whereas midnight UTC would show as the previous day in the
    Americas.
    """
    if value is None:
        return None
    value = value.astimezone(timezone.utc)
    if (value.hour, value.minute, value.second) == (0, 0, 0):
        value = value.replace(hour=12)
    return format_datetime(value)


def due_day(value: str | None) -> str | None:
    """Calendar day of a Graph due date, rounding to the nearest midnight.

    Planner stores a picked day as local midnight converted to UTC, so
    ``2026-03-10T23:00:00Z`` (picked in UTC+1) means 11 March. Noon belongs to
    its own day, which keeps the values written by :func:`due_value` stable.
    """
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    shifted = parsed.astimezone(timezone.utc) + timedelta(hours=12) - timedelta(seconds=1)
    return shifted.date().isoformat()


def _slot_order(slot: str) -> int:
    return int(slot.removeprefix("category")) if slot.startswith("category") else 99


# --------------------------------------------------------------------------- engine


@dataclass
class _Context:
    sync: SyncConfig
    provider: IssueProvider
    state: SyncState
    report: SyncReport
    group_id: str | None
    mapper: CategoryMapper
    resolver: AssigneeResolver
    plan_details_etag: str | None
    buckets: dict[str, str]
    bucket_names: dict[str, str]
    categories: dict[int, list[str]] = field(default_factory=dict)
    index: dict[str, tuple[dict[str, Any], dict[str, Any]]] | None = None
    plan_tasks: dict[str, dict[str, Any]] | None = None
    board: Board | None = None
    changes: int = 0

    @property
    def closed_bucket_id(self) -> str | None:
        name = self.sync.buckets.closed
        return self.buckets.get(name.strip().lower()) if name else None


class SyncEngine:
    """Synchronise the configured repositories with their Planner plans."""

    def __init__(
        self,
        config: AppConfig,
        planner: GraphClient,
        state: StateStore,
        *,
        provider_factory: Callable[[Any], IssueProvider] | None = None,
        dry_run: bool = False,
        force: bool = False,
        reconcile: bool = False,
        full: bool = False,
        direction: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.config = config
        self.planner = planner
        self.state = state
        self.provider_factory = provider_factory or create_provider
        self.dry_run = dry_run
        self.force = force
        self.reconcile = reconcile
        self.full = full
        self.direction = normalize_direction(direction) if direction else None
        self.clock = clock
        self._fake_ids = 0

    # ------------------------------------------------------------------ entry points

    def run(
        self, names: list[str] | None = None, issue_numbers: list[int] | None = None
    ) -> list[SyncReport]:
        return [self.run_sync(sync, issue_numbers) for sync in self.config.select(names)]

    def run_sync(self, sync: SyncConfig, issue_numbers: list[int] | None = None) -> SyncReport:
        if self.direction:
            sync = dataclasses.replace(sync, direction=self.direction)
        report = SyncReport(name=sync.name, dry_run=self.dry_run, direction=sync.direction)
        handler = _ReportHandler(report)
        package_logger = logging.getLogger("git2planner")
        package_logger.addHandler(handler)
        started = self.clock()
        try:
            ctx = self._prepare(sync, report)
            issues = {issue.number: issue for issue in self._collect(ctx, issue_numbers)}
            if sync.pulls:
                self._pull(ctx, issues, single=bool(issue_numbers))
            if sync.pushes:
                self._push(ctx, [issues[number] for number in sorted(issues)])
            if not issue_numbers and report.ok:
                ctx.state.last_run = format_datetime(started)
                ctx.changes += 1
        except Git2PlannerError as exc:
            log.error("%s: %s", sync.name, exc)
            report.errors.append(str(exc))
        finally:
            package_logger.removeHandler(handler)
            if not self.dry_run:
                self.state.save()
        log.info("%s", report.summary())
        return report

    # ------------------------------------------------------------------ preparation

    def _prepare(self, sync: SyncConfig, report: SyncReport) -> _Context:
        provider = self.provider_factory(sync.source)
        plan = self.planner.get_plan(sync.plan_id)
        group_id = self.planner.plan_group_id(plan)
        log.info(
            "%s: %s %s <-> Planner plan %r (%s)",
            sync.name,
            provider.display_name,
            sync.source.repository,
            plan.get("title", sync.plan_id),
            sync.direction,
        )
        state = self.state.sync(sync.name)
        if state.plan_id and state.plan_id != sync.plan_id:
            log.warning(
                "%s: plan changed from %s to %s, previous task links are ignored",
                sync.name,
                state.plan_id,
                sync.plan_id,
            )
            state.issues = {}
            state.last_run = None
        state.plan_id = sync.plan_id

        descriptions: dict[str, Any] = {}
        details_etag: str | None = None
        if sync.categories.mode != "off":
            details = self.planner.get_plan_details(sync.plan_id)
            descriptions = details.get("categoryDescriptions") or {}
            details_etag = etag_of(details)
        buckets: dict[str, str] = {}
        names: dict[str, str] = {}
        for bucket in self.planner.list_buckets(sync.plan_id):
            buckets.setdefault(str(bucket["name"]).strip().lower(), str(bucket["id"]))
            names[str(bucket["id"])] = str(bucket["name"])
        return _Context(
            sync=sync,
            provider=provider,
            state=state,
            report=report,
            group_id=group_id,
            mapper=CategoryMapper(sync.categories, descriptions),
            resolver=AssigneeResolver(sync.assignees, self.planner, provider, group_id),
            board=self._load_board(sync, provider),
            plan_details_etag=details_etag,
            buckets=buckets,
            bucket_names=names,
        )

    @staticmethod
    def _load_board(sync: SyncConfig, provider: IssueProvider) -> Board | None:
        if sync.buckets.strategy != "board":
            return None
        board = provider.load_board(sync.buckets.board)
        log.info(
            "%s: project board %r with columns %s",
            sync.name,
            board.title,
            ", ".join(repr(column) for column in board.columns) or "none",
        )
        return board

    def _collect(self, ctx: _Context, issue_numbers: list[int] | None) -> list[Issue]:
        provider, filters, state = ctx.provider, ctx.sync.filters, ctx.state
        issues: dict[int, Issue] = {}

        if issue_numbers:
            for number in issue_numbers:
                issue = provider.get_issue(number)
                if issue is None:
                    record = state.issues.get(str(number))
                    if record is not None and not record.deleted:
                        self._handle_missing(ctx, number, record)
                    else:
                        log.warning("%s: issue #%d not found", ctx.sync.name, number)
                    continue
                issues[issue.number] = issue
            return [issues[number] for number in sorted(issues)]

        include_prs = filters.include_pull_requests
        for issue in provider.iter_issues("open", include_pull_requests=include_prs):
            issues[issue.number] = issue

        last_run = None if self.full else parse_datetime(state.last_run)
        since = last_run - SINCE_MARGIN if last_run else None
        if filters.state in ("closed", "all") or since is not None:
            for issue in provider.iter_issues("closed", since, include_pull_requests=include_prs):
                issues[issue.number] = issue

        for key, record in list(state.issues.items()):
            if record.deleted or record.missing or record.issue_state != "open":
                continue
            if not key.isdigit() or int(key) in issues:
                continue
            issue = provider.get_issue(int(key))
            if issue is None:
                self._handle_missing(ctx, int(key), record)
            else:
                issues[issue.number] = issue
        log.debug("%s: %d issue(s) fetched", ctx.sync.name, len(issues))
        return [issues[number] for number in sorted(issues)]

    def _will_process(self, ctx: _Context, issue: Issue) -> bool:
        record = ctx.state.issues.get(str(issue.number))
        on_close = ctx.sync.on_close
        if record is None or record.deleted:
            return matches_filters(issue, ctx.sync.filters) and (
                issue.is_open or on_close == "complete"
            )
        return issue.is_open or on_close == "complete"

    def _flush_categories(self, ctx: _Context) -> None:
        pending = dict(ctx.mapper.pending)
        if not pending:
            return
        names = ", ".join(
            f"{slot}={name!r}"
            for slot, name in sorted(pending.items(), key=lambda i: _slot_order(i[0]))
        )
        if self.dry_run:
            log.info("%s: would name Planner categories %s", ctx.sync.name, names)
        else:
            log.info("%s: naming Planner categories %s", ctx.sync.name, names)
            details = self.planner.update_plan_details(
                ctx.sync.plan_id, {"categoryDescriptions": pending}, ctx.plan_details_etag
            )
            ctx.plan_details_etag = etag_of(details)
        ctx.mapper.descriptions.update(pending)
        ctx.mapper.pending.clear()

    def _bucket_id(self, ctx: _Context, name: str) -> str:
        key = name.strip().lower()
        if key in ctx.buckets:
            return ctx.buckets[key]
        buckets = ctx.sync.buckets
        if buckets.create_missing:
            if self.dry_run:
                log.info("%s: would create bucket %r", ctx.sync.name, name)
                bucket_id = self._fake_id("bucket")
            else:
                log.info("%s: creating bucket %r", ctx.sync.name, name)
                bucket_id = str(self.planner.create_bucket(ctx.sync.plan_id, name)["id"])
            ctx.buckets[key] = bucket_id
            ctx.bucket_names[bucket_id] = name
            return bucket_id
        default = buckets.default.strip().lower()
        if key != default:
            log.warning(
                "%s: bucket %r does not exist, using %r", ctx.sync.name, name, buckets.default
            )
            return self._bucket_id(ctx, buckets.default)
        if ctx.buckets:
            return next(iter(ctx.buckets.values()))
        raise Git2PlannerError(
            f"plan {ctx.sync.plan_id} has no buckets and buckets.create_missing is disabled"
        )

    def _fake_id(self, kind: str) -> str:
        self._fake_ids += 1
        return f"dry-run-{kind}-{self._fake_ids}"

    def _plan_tasks(self, ctx: _Context) -> dict[str, dict[str, Any]]:
        if ctx.plan_tasks is None:
            ctx.plan_tasks = {
                str(task["id"]): task for task in self.planner.list_tasks(ctx.sync.plan_id)
            }
        return ctx.plan_tasks

    def _task_details(self, task_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        paths = {f"/planner/tasks/{task_id}/details": task_id for task_id in task_ids}
        if not paths:
            return {}
        return {
            paths[path]: body
            for path, (status, body) in self.planner.batch_get(list(paths)).items()
            if status == 200
        }

    # ------------------------------------------------------------------ desired state

    def _desired(
        self,
        ctx: _Context,
        issue: Issue,
        uploads: dict[str, str],
        *,
        with_references: bool = True,
    ) -> DesiredTask:
        sync = ctx.sync
        categories = ctx.categories.get(issue.number)
        if categories is None:
            categories = ctx.mapper.resolve(issue.labels)
            ctx.categories[issue.number] = categories

        checklist: dict[str, tuple[str, bool]] = {}
        if sync.task.checklist:
            for item_id, title, checked, _ in checklist_items(issue):
                checklist[item_id] = (title, checked)

        description: str | None = None
        if sync.task.description:
            text = to_plain_text(issue.body, strip_task_items=sync.task.checklist)
            if sync.task.footer:
                footer = (
                    f"{FOOTER_PREFIX}{ctx.provider.display_name} "
                    f"{issue.repository}#{issue.number}: {issue.url}"
                )
                text = f"{text}\n\n---\n{footer}" if text else f"---\n{footer}"
            description = _truncate(text, MAX_DESCRIPTION)

        return DesiredTask(
            title=render_title(sync.task.title_template, issue, ctx.provider),
            bucket_id=self._bucket_id(
                ctx,
                bucket_name_for(
                    issue, sync.buckets, ctx.board.column_of(issue) if ctx.board else None
                ),
            ),
            percent_complete=0 if issue.is_open else 100,
            priority=priority_for(issue, sync.priority),
            due=due_value(issue.effective_due) if sync.task.due_date else None,
            assignees=sorted(ctx.resolver.resolve(issue.assignees))[:MAX_ASSIGNEES],
            categories=categories,
            description=description,
            preview=sync.task.preview,
            references=self._references(ctx, issue, uploads) if with_references else {},
            checklist=checklist,
        )

    def _references(
        self, ctx: _Context, issue: Issue, uploads: dict[str, str]
    ) -> dict[str, dict[str, Any]]:
        settings = ctx.sync.attachments
        references: dict[str, dict[str, Any]] = {}
        if settings.link_issue:
            references[reference_key(issue.url)] = self._issue_reference(ctx, issue)
        if settings.mode == "off":
            return references
        names = self._attachment_names(issue.attachments)
        for attachment in issue.attachments:
            if len(references) >= MAX_REFERENCES:
                log.warning(
                    "%s: issue #%d has more than %d attachments, the rest are skipped",
                    ctx.sync.name,
                    issue.number,
                    MAX_REFERENCES,
                )
                break
            url = attachment.url
            if settings.mode == "upload":
                url = self._upload(ctx, issue, attachment, names[attachment.url], uploads) or url
            references[reference_key(url)] = {
                "@odata.type": REFERENCE_TYPE,
                "alias": _truncate(attachment.name, MAX_ALIAS),
                "type": reference_type(attachment.name),
            }
        return references

    @staticmethod
    def _issue_reference(ctx: _Context, issue: Issue) -> dict[str, Any]:
        return {
            "@odata.type": REFERENCE_TYPE,
            "alias": f"{ctx.provider.display_name} issue #{issue.number}",
            "type": "Other",
        }

    @staticmethod
    def _attachment_names(attachments: list[Attachment]) -> dict[str, str]:
        used: Counter[str] = Counter()
        names: dict[str, str] = {}
        for attachment in attachments:
            name = sanitize_filename(attachment.name)
            used[name.lower()] += 1
            if used[name.lower()] > 1:
                stem, extension = posixpath.splitext(name)
                name = f"{stem}-{used[name.lower()]}{extension}"
            names[attachment.url] = name
        return names

    def _upload(
        self,
        ctx: _Context,
        issue: Issue,
        attachment: Attachment,
        filename: str,
        uploads: dict[str, str],
    ) -> str | None:
        if attachment.url in uploads:
            return uploads[attachment.url]
        if ctx.group_id is None:
            log.warning("%s: plan has no group, cannot upload attachments", ctx.sync.name)
            return None
        values = _template_values(issue, ctx.provider)
        folder = sanitize_folder(ctx.sync.attachments.folder.format(**values))
        if self.dry_run:
            log.info("%s: would upload %s to %s/", ctx.sync.name, attachment.name, folder)
            return None
        max_bytes = int(ctx.sync.attachments.max_size_mb * 1024 * 1024)
        try:
            content, content_type = ctx.provider.download(attachment, max_bytes)
            name = sanitize_filename(filename, content_type=content_type)
            item = self.planner.upload_group_file(
                ctx.group_id, f"{folder}/{name}" if folder else name, content, content_type
            )
        except (AttachmentError, HTTPError) as exc:
            log.warning(
                "%s: issue #%d: attachment %s kept as a link: %s",
                ctx.sync.name,
                issue.number,
                attachment.name,
                exc,
            )
            return None
        web_url = item.get("webUrl")
        if not web_url:
            return None
        uploads[attachment.url] = str(web_url)
        log.info("%s: issue #%d: uploaded %s", ctx.sync.name, issue.number, name)
        return str(web_url)

    # ------------------------------------------------------------------ git -> planner

    def _push(self, ctx: _Context, issues: list[Issue]) -> None:
        for issue in issues:
            if self._will_process(ctx, issue):
                ctx.categories[issue.number] = ctx.mapper.resolve(issue.labels)
        self._flush_categories(ctx)
        for issue in issues:
            try:
                self._process(ctx, issue)
            except Git2PlannerError as exc:
                message = f"issue #{issue.number}: {exc}"
                log.error("%s: %s", ctx.sync.name, message)
                ctx.report.errors.append(message)
            self._maybe_save(ctx)
        self._flush_categories(ctx)

    def _process(self, ctx: _Context, issue: Issue) -> None:
        sync = ctx.sync
        key = str(issue.number)
        record = ctx.state.issues.get(key)

        if record is not None and record.deleted:
            recreate = (
                (record.deleted == "closed" and issue.is_open)
                or (record.deleted == "planner" and sync.recreate_deleted)
                or record.deleted == "missing"
            )
            if not recreate:
                ctx.report.skipped += 1
                return
            log.info("%s: issue #%d needs a new task", sync.name, issue.number)
            record = None

        if record is None:
            if not matches_filters(issue, sync.filters):
                ctx.report.skipped += 1
                return
            if not issue.is_open and sync.on_close != "complete":
                ctx.report.skipped += 1
                return
            existing = self._find_existing(ctx, issue)
            if existing is not None:
                self._link(ctx, issue, *existing)
            else:
                self._create(ctx, issue)
            return

        if record.missing:
            record.missing = False
            ctx.changes += 1

        if not issue.is_open and sync.on_close == "delete":
            self._delete(ctx, issue.number, record, "closed")
            return
        if not issue.is_open and sync.on_close == "ignore":
            if record.issue_state != "closed":
                record.issue_state = "closed"
                ctx.changes += 1
            ctx.report.unchanged += 1
            return
        self._update(ctx, issue, record)

    def _task_patch(
        self, desired: DesiredTask, record: TaskRecord | None, *, move_bucket: bool
    ) -> dict[str, Any]:
        full = record is None or self.force
        patch: dict[str, Any] = {}

        if record is None or full or desired.title != record.title:
            patch["title"] = desired.title
        if move_bucket and (self.force or record is None or desired.bucket_id != record.bucket_id):
            patch["bucketId"] = desired.bucket_id
        if record is None or full or desired.percent_complete != record.percent_complete:
            patch["percentComplete"] = desired.percent_complete
        if record is None or full or desired.priority != record.priority:
            patch["priority"] = desired.priority
        previous_due = record.due if record else None
        if desired.due != previous_due or (full and desired.due is not None):
            patch["dueDateTime"] = desired.due

        previous = set(record.assignees) if record else set()
        wanted = set(desired.assignees)
        assignments: dict[str, Any] = {
            user: dict(ASSIGNMENT) for user in sorted(wanted if full else wanted - previous)
        }
        assignments.update(dict.fromkeys(sorted(previous - wanted)))
        if assignments:
            patch["assignments"] = assignments

        previous = set(record.categories) if record else set()
        wanted = set(desired.categories)
        applied = dict.fromkeys(sorted(wanted if full else wanted - previous), True)
        applied.update(dict.fromkeys(sorted(previous - wanted), False))
        if applied:
            patch["appliedCategories"] = applied
        return patch

    def _details_patch(self, desired: DesiredTask, record: TaskRecord | None) -> dict[str, Any]:
        full = record is None or self.force
        patch: dict[str, Any] = {}
        if desired.description is not None and (
            record is None or full or desired.description_hash != record.description_hash
        ):
            patch["description"] = desired.description
        if record is None or full or desired.preview != record.preview:
            patch["previewType"] = desired.preview

        previous_refs = record.references if record else {}
        references: dict[str, Any] = {
            key: value
            for key, value in desired.references.items()
            if full or previous_refs.get(key) != value["alias"]
        }
        references.update({key: None for key in previous_refs if key not in desired.references})
        if references:
            patch["references"] = references

        previous_items = record.checklist if record else {}
        checklist: dict[str, Any] = {
            item_id: {"@odata.type": CHECKLIST_TYPE, "title": title, "isChecked": checked}
            for item_id, (title, checked) in desired.checklist.items()
            if full or previous_items.get(item_id) != checked
        }
        checklist.update(
            {item_id: None for item_id in previous_items if item_id not in desired.checklist}
        )
        if checklist:
            patch["checklist"] = checklist
        return patch

    @staticmethod
    def _commit_task(record: TaskRecord, desired: DesiredTask) -> None:
        record.title = desired.title
        record.bucket_id = desired.bucket_id
        record.percent_complete = desired.percent_complete
        record.priority = desired.priority
        record.due = desired.due
        record.assignees = list(desired.assignees)
        record.categories = list(desired.categories)

    @staticmethod
    def _commit_details(record: TaskRecord, desired: DesiredTask) -> None:
        if desired.description is not None:
            record.description_hash = desired.description_hash
        record.preview = desired.preview
        record.references = {key: str(value["alias"]) for key, value in desired.references.items()}
        record.checklist = desired.checklist_states

    def _write_details(self, ctx: _Context, record: TaskRecord, patch: dict[str, Any]) -> None:
        if self.dry_run:
            log.debug("%s: would update details of %s: %s", ctx.sync.name, record.task_id, patch)
            return
        try:
            details = self.planner.update_task_details(record.task_id, patch, record.details_etag)
        except HTTPError as exc:
            if exc.status != 400 or "checklist" not in patch:
                raise
            log.warning(
                "%s: Planner rejected the checklist of %s (%s); retrying without it",
                ctx.sync.name,
                record.task_id,
                exc,
            )
            patch = {key: value for key, value in patch.items() if key != "checklist"}
            details = self.planner.update_task_details(record.task_id, patch, None)
        record.details_etag = etag_of(details)

    def _create(self, ctx: _Context, issue: Issue) -> None:
        uploads: dict[str, str] = {}
        desired = self._desired(ctx, issue, uploads)
        payload = {
            "planId": ctx.sync.plan_id,
            **self._task_patch(desired, None, move_bucket=True),
        }
        if self.dry_run:
            log.info("%s: would create task %r", ctx.sync.name, desired.title)
            task: dict[str, Any] = {"id": self._fake_id("task")}
        else:
            task = self.planner.create_task(payload)
            log.info("%s: created task %r", ctx.sync.name, desired.title)
        record = TaskRecord(
            task_id=str(task["id"]),
            issue_url=issue.url,
            issue_state=issue.state,
            etag=etag_of(task),
            uploads=uploads,
            synced_at=format_datetime(self.clock()),
        )
        self._commit_task(record, desired)
        ctx.state.issues[str(issue.number)] = record
        ctx.report.created += 1
        self._save_now(ctx)

        details_patch = self._details_patch(desired, None)
        if details_patch:
            if not self.dry_run:
                record.details_etag = etag_of(self.planner.get_task_details(record.task_id))
            self._write_details(ctx, record, details_patch)
        self._commit_details(record, desired)
        ctx.changes += 1

    def _link(
        self, ctx: _Context, issue: Issue, task: dict[str, Any], details: dict[str, Any]
    ) -> None:
        """Adopt a task that already exists in the plan (lost or missing state file)."""
        record = TaskRecord(
            task_id=str(task["id"]),
            issue_url=issue.url,
            issue_state=issue.state,
            etag=etag_of(task),
            details_etag=etag_of(details),
        )
        desired = self._desired(ctx, issue, record.uploads)
        task_patch = self._task_patch(desired, None, move_bucket=self.force)
        details_patch = self._details_patch(desired, None)
        verb = "would link" if self.dry_run else "linked"
        log.info("%s: %s issue #%d to task %s", ctx.sync.name, verb, issue.number, task["id"])
        if task_patch and not self.dry_run:
            record.etag = etag_of(self.planner.update_task(record.task_id, task_patch, record.etag))
        self._commit_task(record, desired)
        ctx.state.issues[str(issue.number)] = record
        self._save_now(ctx)
        if details_patch:
            self._write_details(ctx, record, details_patch)
        self._commit_details(record, desired)
        record.synced_at = format_datetime(self.clock())
        ctx.report.linked += 1
        ctx.changes += 1

    def _update(self, ctx: _Context, issue: Issue, record: TaskRecord) -> None:
        desired = self._desired(ctx, issue, record.uploads)
        task_patch = self._task_patch(desired, record, move_bucket=True)
        details_patch = self._details_patch(desired, record)
        previous_percent = record.percent_complete
        if not task_patch and not details_patch:
            if record.issue_state != issue.state:
                record.issue_state = issue.state
                ctx.changes += 1
            ctx.report.unchanged += 1
            return

        fields = ", ".join(sorted({*task_patch, *details_patch}))
        try:
            if task_patch and not self.dry_run:
                task = self.planner.update_task(record.task_id, task_patch, record.etag)
                record.etag = etag_of(task)
            self._commit_task(record, desired)
            if details_patch:
                self._write_details(ctx, record, details_patch)
            self._commit_details(record, desired)
        except NotFoundError:
            self._task_vanished(ctx, issue, record)
            return

        verb = "would update" if self.dry_run else "updated"
        log.info("%s: %s task %r (%s)", ctx.sync.name, verb, desired.title, fields)
        record.issue_state = issue.state
        record.synced_at = format_datetime(self.clock())
        ctx.report.updated += 1
        if desired.percent_complete == 100 and previous_percent != 100:
            ctx.report.completed += 1
        elif desired.percent_complete == 0 and previous_percent == 100:
            ctx.report.reopened += 1
        ctx.changes += 1

    def _task_vanished(self, ctx: _Context, issue: Issue, record: TaskRecord) -> None:
        log.warning(
            "%s: task for issue #%d was deleted in Planner%s",
            ctx.sync.name,
            issue.number,
            ", recreating it" if ctx.sync.recreate_deleted else "",
        )
        record.deleted = "planner"
        ctx.changes += 1
        if ctx.sync.recreate_deleted:
            del ctx.state.issues[str(issue.number)]
            self._create(ctx, issue)
        else:
            ctx.report.skipped += 1

    def _delete(self, ctx: _Context, number: int, record: TaskRecord, reason: str) -> None:
        if self.dry_run:
            log.info("%s: would delete task for issue #%d", ctx.sync.name, number)
        else:
            with contextlib.suppress(NotFoundError):
                self.planner.delete_task(record.task_id, record.etag)
            log.info("%s: deleted task for issue #%d (%s)", ctx.sync.name, number, reason)
        record.deleted = reason
        record.etag = None
        record.details_etag = None
        record.synced_at = format_datetime(self.clock())
        ctx.report.deleted += 1
        ctx.changes += 1

    def _handle_missing(self, ctx: _Context, number: int, record: TaskRecord) -> None:
        policy = ctx.sync.on_missing
        if policy == "delete":
            self._delete(ctx, number, record, "missing")
            return
        if policy == "complete" and record.percent_complete != 100:
            if self.dry_run:
                log.info("%s: would complete task for vanished issue #%d", ctx.sync.name, number)
            else:
                try:
                    task = self.planner.update_task(
                        record.task_id, {"percentComplete": 100}, record.etag
                    )
                    record.etag = etag_of(task)
                except NotFoundError:
                    record.deleted = "planner"
            record.percent_complete = 100
            ctx.report.updated += 1
            ctx.report.completed += 1
        log.warning(
            "%s: issue #%d no longer exists in %s (deleted or transferred)",
            ctx.sync.name,
            number,
            ctx.provider.display_name,
        )
        record.missing = True
        ctx.changes += 1

    def _find_existing(
        self, ctx: _Context, issue: Issue
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        if ctx.index is None:
            ctx.index = self._build_index(ctx)
        return ctx.index.pop(issue.url.rstrip("/").lower(), None)

    def _build_index(self, ctx: _Context) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
        """Map issue URLs to tasks already in the plan, found through their links."""
        active = [record for record in ctx.state.issues.values() if not record.deleted]
        if active and not self.reconcile:
            return {}
        known = {record.task_id for record in active}
        tasks = [task for task_id, task in self._plan_tasks(ctx).items() if task_id not in known]
        if not tasks:
            return {}
        log.info("%s: looking for existing tasks among %d task(s)", ctx.sync.name, len(tasks))
        details = self._task_details(str(task["id"]) for task in tasks)
        by_id = {str(task["id"]): task for task in tasks}
        index: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for task_id, body in details.items():
            for url in self._linked_urls(body):
                index.setdefault(url.rstrip("/").lower(), (by_id[task_id], body))
        return index

    @staticmethod
    def _linked_urls(details: dict[str, Any]) -> list[str]:
        urls = [decode_reference_key(key) for key in (details.get("references") or {})]
        urls += _URL.findall(str(details.get("description") or ""))
        return urls

    # ------------------------------------------------------------------ planner -> git

    def _pull(self, ctx: _Context, issues: dict[int, Issue], *, single: bool) -> None:
        settings = ctx.sync.planner_to_git
        tasks = self._plan_tasks(ctx)
        wanted_fields = set(settings.fields)
        active = {
            key: record
            for key, record in ctx.state.issues.items()
            if not record.deleted and key.isdigit() and (not single or int(key) in issues)
        }
        detail_ids: list[str] = []
        if wanted_fields & {"checklist", "description"}:
            detail_ids += [record.task_id for record in active.values() if record.task_id in tasks]
        new_tasks: list[dict[str, Any]] = []
        if settings.create_issues and not single:
            tracked = {
                record.task_id
                for state in self.state.syncs.values()
                for record in state.issues.values()
            }
            new_tasks = [
                task
                for task_id, task in tasks.items()
                if task_id not in tracked and self._wants_issue(ctx, task)
            ]
            detail_ids += [str(task["id"]) for task in new_tasks]
        details = self._task_details(detail_ids)
        if "assignees" in wanted_fields:
            for key in active:
                issue = issues.get(int(key))
                if issue is not None:
                    ctx.resolver.resolve(issue.assignees)

        for key, record in active.items():
            number = int(key)
            try:
                task = tasks.get(record.task_id)
                if task is None:
                    self._pulled_deletion(ctx, number, record, issues)
                else:
                    self._pull_task(ctx, number, record, task, details.get(record.task_id), issues)
            except Git2PlannerError as exc:
                message = f"issue #{number}: {exc}"
                log.error("%s: %s", ctx.sync.name, message)
                ctx.report.errors.append(message)
            self._maybe_save(ctx)

        for task in new_tasks:
            try:
                self._issue_from_task(ctx, task, details.get(str(task["id"])), issues)
            except Git2PlannerError as exc:
                message = f"task {task.get('title')!r}: {exc}"
                log.error("%s: %s", ctx.sync.name, message)
                ctx.report.errors.append(message)

    def _wants_issue(self, ctx: _Context, task: dict[str, Any]) -> bool:
        settings = ctx.sync.planner_to_git
        if not settings.include_completed and int(task.get("percentComplete") or 0) == 100:
            return False
        if settings.buckets:
            name = ctx.bucket_names.get(str(task.get("bucketId")), "")
            return name.strip().lower() in {bucket.strip().lower() for bucket in settings.buckets}
        return True

    def _pulled_deletion(
        self, ctx: _Context, number: int, record: TaskRecord, issues: dict[int, Issue]
    ) -> None:
        close = ctx.sync.planner_to_git.close_deleted
        log.warning(
            "%s: task for issue #%d was deleted in Planner%s",
            ctx.sync.name,
            number,
            ", closing the issue" if close else "",
        )
        record.deleted = "planner"
        ctx.changes += 1
        if not close:
            return
        issue = issues.get(number) or ctx.provider.get_issue(number)
        record.deleted = "closed"
        if issue is None or not issue.is_open:
            return
        if self.dry_run:
            log.info("%s: would close issue #%d", ctx.sync.name, number)
        else:
            issues[number] = ctx.provider.update_issue(number, IssueChanges(state="closed"))
        record.issue_state = "closed"
        ctx.report.pulled += 1

    def _planner_changes(
        self,
        ctx: _Context,
        record: TaskRecord,
        task: dict[str, Any],
        details: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Fields edited in Planner since the baseline (only those Git can represent)."""
        sync = ctx.sync
        wanted = set(sync.planner_to_git.fields)
        changes: dict[str, Any] = {}
        bucket_id = str(task.get("bucketId") or "")
        closed_bucket = ctx.closed_bucket_id

        if "state" in wanted:
            moved_to_closed = bool(
                closed_bucket and bucket_id == closed_bucket and record.bucket_id != closed_bucket
            )
            done_now = int(task.get("percentComplete") or 0) == 100 or moved_to_closed
            if done_now != (record.percent_complete == 100):
                changes["state"] = "closed" if done_now else "open"

        title = str(task.get("title") or "").strip()
        if "title" in wanted and title and title != record.title:
            changes["title"] = title

        if "assignees" in wanted:
            current = {
                user.lower() for user, value in (task.get("assignments") or {}).items() if value
            }
            baseline = set(record.assignees)
            added = {user for user in current - baseline if ctx.resolver.login_for(user)}
            removed = baseline - current
            if added or removed:
                changes["assignees"] = (added, removed)

        if "labels" in wanted:
            applied = {
                slot for slot, value in (task.get("appliedCategories") or {}).items() if value
            }
            baseline = set(record.categories)
            added = {slot for slot in applied - baseline if ctx.mapper.label_for(slot)}
            removed = {slot for slot in baseline - applied if ctx.mapper.label_for(slot)}
            if added or removed:
                changes["categories"] = (added, removed)
            priority = task.get("priority")
            if (
                sync.priority.mapping
                and isinstance(priority, int)
                and record.priority is not None
                and priority_band(priority) != priority_band(record.priority)
            ):
                changes["priority"] = priority

        strategy = sync.buckets.strategy
        if (
            ("bucket" in wanted or (strategy == "label" and "labels" in wanted))
            and strategy in ("label", "board")
            and bucket_id
            and bucket_id not in (record.bucket_id, closed_bucket)
        ):
            name = ctx.bucket_names.get(bucket_id, "")
            is_default = name.strip().lower() == sync.buckets.default.strip().lower()
            if strategy == "label":
                representable = bool(bucket_label(sync.buckets, name)) or is_default
            else:
                board = ctx.board
                representable = board is not None and (
                    column_for_bucket(sync.buckets, name, board.columns) is not None
                    or (is_default and board.can_clear)
                )
            if representable:
                changes["bucket"] = bucket_id

        if (
            "due_date" in wanted
            and ctx.provider.supports_due_date
            and due_day(task.get("dueDateTime")) != due_day(record.due)
        ):
            changes["due"] = task.get("dueDateTime")

        if "checklist" in wanted and details is not None and sync.task.checklist:
            items = details.get("checklist") or {}
            toggles = {
                item_id: bool(item.get("isChecked"))
                for item_id, item in items.items()
                if item_id in record.checklist
                and bool(item.get("isChecked")) != record.checklist[item_id]
            }
            ordered = sorted(items.items(), key=lambda pair: str(pair[1].get("orderHint") or ""))
            new = [
                (item_id, str(item["title"]).strip(), bool(item.get("isChecked")))
                for item_id, item in ordered
                if item_id not in record.checklist and str(item.get("title") or "").strip()
            ]
            if toggles or new:
                changes["checklist"] = (toggles, new)

        if "description" in wanted and details is not None and record.description_hash:
            description = str(details.get("description") or "")
            if text_hash(description) != record.description_hash:
                changes["description"] = description
        return changes

    @staticmethod
    def _git_changed(name: str, desired: DesiredTask, record: TaskRecord) -> bool:
        """Whether a field also changed on the Git side since the baseline."""
        if name == "state":
            return desired.percent_complete != record.percent_complete
        if name == "title":
            return desired.title != record.title
        if name == "assignees":
            return set(desired.assignees) != set(record.assignees)
        if name == "categories":
            return set(desired.categories) != set(record.categories)
        if name == "priority":
            return desired.priority != record.priority
        if name == "bucket":
            return desired.bucket_id != record.bucket_id
        if name == "due":
            return due_day(desired.due) != due_day(record.due)
        if name == "checklist":
            return desired.checklist_states != record.checklist
        if name == "description":
            return desired.description_hash != record.description_hash
        return False

    def _pull_task(
        self,
        ctx: _Context,
        number: int,
        record: TaskRecord,
        task: dict[str, Any],
        details: dict[str, Any] | None,
        issues: dict[int, Issue],
    ) -> None:
        record.etag = etag_of(task) or record.etag
        if details is not None:
            record.details_etag = etag_of(details) or record.details_etag
        changes = self._planner_changes(ctx, record, task, details)
        if not changes:
            return
        issue = issues.get(number) or ctx.provider.get_issue(number)
        if issue is None:
            return
        issues[number] = issue

        git_side = self._desired(ctx, issue, record.uploads, with_references=False)
        accepted: dict[str, Any] = {}
        for name, value in changes.items():
            if self._git_changed(name, git_side, record):
                ctx.report.conflicts += 1
                if ctx.sync.conflicts == "git":
                    log.info(
                        "%s: issue #%d: %s changed in both places, keeping the %s value",
                        ctx.sync.name,
                        number,
                        name,
                        ctx.provider.display_name,
                    )
                    continue
                log.info(
                    "%s: issue #%d: %s changed in both places, keeping the Planner value",
                    ctx.sync.name,
                    number,
                    name,
                )
            accepted[name] = value
        if not accepted:
            return

        issue_changes, baseline = self._issue_changes(ctx, issue, record, accepted, task)
        described = ", ".join(sorted(accepted))
        if self.dry_run:
            log.info(
                "%s: would update issue #%d from Planner (%s)", ctx.sync.name, number, described
            )
            ctx.report.pulled += 1
            return
        changed = False
        if not issue_changes.is_empty():
            issues[number] = ctx.provider.update_issue(number, issue_changes)
            changed = True
        if ctx.board is not None:
            buckets = ctx.sync.buckets
            column: str | None = None
            move = False
            if "bucket" in accepted:
                name = ctx.bucket_names.get(accepted["bucket"], "")
                column = column_for_bucket(buckets, name, ctx.board.columns)
                move = True
            elif accepted.get("state") == "closed" and buckets.closed:
                column = column_for_bucket(buckets, buckets.closed, ctx.board.columns)
                move = column is not None
            if move:
                issues[number] = ctx.board.move(issues[number], column)
                changed = True
        if changed:
            log.info("%s: updated issue #%d from Planner (%s)", ctx.sync.name, number, described)
            ctx.report.pulled += 1
        if "due" in accepted:
            updated = issues[number]
            baseline["due"] = due_value(updated.effective_due) if ctx.sync.task.due_date else None
        for attribute, value in baseline.items():
            setattr(record, attribute, value)
        record.issue_state = issues[number].state
        record.synced_at = format_datetime(self.clock())
        ctx.changes += 1

    def _issue_changes(
        self,
        ctx: _Context,
        issue: Issue,
        record: TaskRecord,
        accepted: dict[str, Any],
        task: dict[str, Any],
    ) -> tuple[IssueChanges, dict[str, Any]]:
        """Translate accepted Planner edits into issue changes and new baseline values."""
        sync = ctx.sync
        changes = IssueChanges()
        baseline: dict[str, Any] = {}
        labels = list(issue.label_names)

        def add_label(name: str) -> None:
            if name.lower() not in {label.lower() for label in labels}:
                labels.append(name)

        def remove_label(name: str) -> None:
            labels[:] = [label for label in labels if label.lower() != name.lower()]

        if "state" in accepted:
            changes.state = accepted["state"]
            # A task dragged into the "closed" bucket but not ticked off keeps a
            # baseline of 0 % so that the next push marks it as completed.
            ticked = int(task.get("percentComplete") or 0) == 100
            baseline["percent_complete"] = 100 if accepted["state"] == "closed" and ticked else 0
            if accepted["state"] == "closed" and task.get("bucketId") == ctx.closed_bucket_id:
                baseline["bucket_id"] = ctx.closed_bucket_id
        if "title" in accepted:
            changes.title = extract_title(
                sync.task.title_template, accepted["title"], issue, ctx.provider
            )
            baseline["title"] = accepted["title"]
        if "assignees" in accepted:
            added, removed = accepted["assignees"]
            logins = [user.login for user in issue.assignees]
            for object_id in sorted(added):
                login = ctx.resolver.login_for(object_id)
                if login and login.lower() not in {name.lower() for name in logins}:
                    logins.append(login)
            for object_id in removed:
                login = ctx.resolver.login_for(object_id)
                if login:
                    logins = [name for name in logins if name.lower() != login.lower()]
            changes.assignees = logins
            baseline["assignees"] = sorted((set(record.assignees) | added) - removed)
        if "categories" in accepted:
            added, removed = accepted["categories"]
            for slot in sorted(added, key=_slot_order):
                label = ctx.mapper.label_for(slot)
                if label:
                    add_label(label)
                    changes.label_colors[label] = ctx.mapper.color_of(slot)
            for slot in removed:
                label = ctx.mapper.label_for(slot)
                if label:
                    remove_label(label)
            baseline["categories"] = sorted(
                (set(record.categories) | added) - removed, key=_slot_order
            )
        if "priority" in accepted:
            for label in sync.priority.mapping:
                remove_label(label)
            new_label = priority_label(sync.priority, accepted["priority"])
            if new_label:
                add_label(new_label)
            baseline["priority"] = (
                sync.priority.mapping[new_label] if new_label else sync.priority.default
            )
        if "bucket" in accepted:
            if sync.buckets.strategy == "label":
                for label in sync.buckets.mapping:
                    remove_label(label)
                name = ctx.bucket_names.get(accepted["bucket"], "")
                new_label = bucket_label(sync.buckets, name)
                if new_label:
                    add_label(new_label)
            baseline["bucket_id"] = accepted["bucket"]
        if labels != issue.label_names:
            changes.labels = labels
        if "due" in accepted:
            day = due_day(accepted["due"])
            if day is None:
                changes.clear_due_date = True
            else:
                changes.due_date = datetime.fromisoformat(day).replace(hour=12, tzinfo=timezone.utc)

        body = issue.body
        if "checklist" in accepted:
            toggles, new = accepted["checklist"]
            lines = {
                line: toggles[item_id]
                for item_id, _, _, line in checklist_items(issue)
                if item_id in toggles
            }
            body = set_task_item_states(body, lines)
            body = append_task_items(body, [(title, checked) for _, title, checked in new])
            states = dict(record.checklist)
            states.update(toggles)
            states.update({item_id: checked for item_id, _, checked in new})
            baseline["checklist"] = states
        if "description" in accepted:
            text = strip_footer(accepted["description"])
            kept = task_item_lines(body) if sync.task.checklist else []
            body = f"{text}\n\n" + "\n".join(kept) if kept and text else (text or "\n".join(kept))
            baseline["description_hash"] = text_hash(accepted["description"])
        if body != issue.body:
            changes.body = body
        return changes, baseline

    def _issue_from_task(
        self,
        ctx: _Context,
        task: dict[str, Any],
        details: dict[str, Any] | None,
        issues: dict[int, Issue],
    ) -> None:
        sync = ctx.sync
        task_id = str(task["id"])
        if details is None:
            details = self.planner.get_task_details(task_id)
        project = ctx.provider.project_web_url.lower() + "/"
        description_text = str(details.get("description") or "")
        if _FOOTER.search(description_text) or any(
            url.lower().startswith(project) for url in self._linked_urls(details)
        ):
            log.debug("%s: task %s already belongs to an issue, skipped", sync.name, task_id)
            return

        title = str(task.get("title") or "").strip() or "Untitled task"
        description = strip_footer(str(details.get("description") or ""))
        items = sorted(
            (details.get("checklist") or {}).items(),
            key=lambda pair: str(pair[1].get("orderHint") or ""),
        )
        body = append_task_items(
            description,
            [
                (str(item["title"]).strip(), bool(item.get("isChecked")))
                for _, item in items
                if str(item.get("title") or "").strip()
            ],
        )

        labels: list[str] = []
        colors: dict[str, str] = {}
        for slot, value in sorted((task.get("appliedCategories") or {}).items()):
            label = ctx.mapper.label_for(slot) if value else None
            if label and label not in labels:
                labels.append(label)
                colors[label] = ctx.mapper.color_of(slot)
        priority = task.get("priority")
        if isinstance(priority, int) and priority_band(priority) != priority_band(
            sync.priority.default
        ):
            label = priority_label(sync.priority, priority)
            if label and label not in labels:
                labels.append(label)
        bucket_name = ctx.bucket_names.get(str(task.get("bucketId")), "")
        label = bucket_label(sync.buckets, bucket_name)
        if label and label not in labels:
            labels.append(label)

        assignees = [
            login
            for user, value in (task.get("assignments") or {}).items()
            if value and (login := ctx.resolver.login_for(user))
        ]
        day = due_day(task.get("dueDateTime"))
        due = (
            datetime.fromisoformat(day).replace(hour=12, tzinfo=timezone.utc)
            if day and ctx.provider.supports_due_date
            else None
        )
        done = int(task.get("percentComplete") or 0) == 100
        changes = IssueChanges(
            title=title,
            body=body,
            labels=labels,
            assignees=assignees,
            due_date=due,
            state="closed" if done else None,
            label_colors=colors,
        )
        if self.dry_run:
            log.info("%s: would create an issue from task %r", sync.name, title)
            ctx.report.issues_created += 1
            return

        issue = ctx.provider.create_issue(changes)
        log.info("%s: created issue #%d from task %r", sync.name, issue.number, title)
        if ctx.board is not None:
            column = column_for_bucket(sync.buckets, bucket_name, ctx.board.columns)
            if column is not None:
                issue = ctx.board.move(issue, column)
        record = TaskRecord(
            task_id=task_id,
            issue_url=issue.url,
            issue_state=issue.state,
            etag=etag_of(task),
            details_etag=etag_of(details),
            origin="planner",
            synced_at=format_datetime(self.clock()),
        )
        desired = self._desired(ctx, issue, record.uploads, with_references=False)
        # Planner already holds these values: adopt them as the baseline so that
        # only the template title, the description footer, the issue link and the
        # checklist item ids are written back.
        self._commit_task(record, desired)
        self._commit_details(record, desired)
        record.title = title
        record.description_hash = text_hash(str(details.get("description") or ""))
        record.checklist = {
            item_id: bool(item.get("isChecked")) for item_id, item in items if item.get("title")
        }
        ctx.state.issues[str(issue.number)] = record
        issues[issue.number] = issue
        ctx.report.issues_created += 1
        self._save_now(ctx)

        if not sync.pushes and sync.attachments.link_issue:
            reference = self._issue_reference(ctx, issue)
            self._write_details(ctx, record, {"references": {reference_key(issue.url): reference}})
            record.references = {reference_key(issue.url): str(reference["alias"])}
            ctx.changes += 1

    # ------------------------------------------------------------------ persistence

    def _save_now(self, ctx: _Context) -> None:
        if not self.dry_run:
            self.state.save()
            ctx.changes = 0

    def _maybe_save(self, ctx: _Context) -> None:
        if ctx.changes >= SAVE_EVERY:
            self._save_now(ctx)
