"""Mapping of Git concepts (labels, milestones, users) onto Planner concepts."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import AssigneeConfig, BucketConfig, CategoryConfig, PriorityConfig, is_category_slot
from .errors import HTTPError, PermissionDeniedError
from .models import GitUser, Issue, Label

log = logging.getLogger(__name__)

CATEGORY_SLOTS = tuple(f"category{index}" for index in range(1, 26))

#: Planner's fixed label colours (approximate RGB) used to pick a slot for a label colour.
PLANNER_CATEGORY_COLORS: dict[str, tuple[str, str]] = {
    "category1": ("Pink", "e43ba6"),
    "category2": ("Red", "d13438"),
    "category3": ("Yellow", "fde300"),
    "category4": ("Green", "107c10"),
    "category5": ("Blue", "0078d4"),
    "category6": ("Purple", "5c2e91"),
    "category7": ("Bronze", "a74109"),
    "category8": ("Lime", "73aa24"),
    "category9": ("Aqua", "00b7c3"),
    "category10": ("Gray", "7a7574"),
    "category11": ("Silver", "a19f9d"),
    "category12": ("Brown", "8e562e"),
    "category13": ("Cranberry", "c50f1f"),
    "category14": ("Orange", "f7630c"),
    "category15": ("Peach", "ff8c00"),
    "category16": ("Marigold", "eaa300"),
    "category17": ("Light green", "13a10e"),
    "category18": ("Dark green", "0b6a0b"),
    "category19": ("Teal", "038387"),
    "category20": ("Light blue", "3a96dd"),
    "category21": ("Dark blue", "003966"),
    "category22": ("Lavender", "7160e8"),
    "category23": ("Plum", "77004d"),
    "category24": ("Light gray", "c8c6c4"),
    "category25": ("Dark gray", "484644"),
}

_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def color_distance(first: str, second: str) -> float:
    """Perceptually weighted RGB distance ("redmean" approximation)."""
    r1, g1, b1 = _rgb(first)
    r2, g2, b2 = _rgb(second)
    mean_red = (r1 + r2) / 2
    dr, dg, db = r1 - r2, g1 - g2, b1 - b2
    return float(
        ((2 + mean_red / 256) * dr * dr + 4 * dg * dg + (2 + (255 - mean_red) / 256) * db * db)
        ** 0.5
    )


# --------------------------------------------------------------------------- categories


class CategoryMapper:
    """Translate issue labels into Planner category slots (``category1`` … ``category25``).

    Category names live in the plan details (``categoryDescriptions``). Labels
    are matched by name; when ``create_missing`` is enabled an unused slot is
    claimed for new names, preferring the slot whose colour is closest to the
    label colour.
    """

    def __init__(self, config: CategoryConfig, descriptions: Mapping[str, Any] | None) -> None:
        self.config = config
        self.descriptions: dict[str, str | None] = {
            slot: (str(value) if value else None)
            for slot, value in (descriptions or {}).items()
            if slot in CATEGORY_SLOTS
        }
        self.pending: dict[str, str] = {}
        self._mapping = {key.lower(): value for key, value in config.mapping.items()}
        self._exclude = {name.lower() for name in config.exclude}
        self._warned: set[str] = set()

    def _slot_by_name(self, name: str) -> str | None:
        wanted = name.strip().lower()
        for slot in CATEGORY_SLOTS:
            description = self.pending.get(slot) or self.descriptions.get(slot)
            if description and description.strip().lower() == wanted:
                return slot
        return None

    def free_slots(self) -> list[str]:
        return [
            slot
            for slot in CATEGORY_SLOTS
            if not self.descriptions.get(slot) and slot not in self.pending
        ]

    def _allocate(self, name: str, color: str | None) -> str | None:
        free = self.free_slots()
        if not free:
            if name.lower() not in self._warned:
                self._warned.add(name.lower())
                log.warning("no free Planner category left for label %r (all 25 are named)", name)
            return None
        slot = free[0]
        if color:
            slot = min(free, key=lambda s: color_distance(color, PLANNER_CATEGORY_COLORS[s][1]))
        self.pending[slot] = name
        log.debug("label %r -> new category %s (%s)", name, slot, PLANNER_CATEGORY_COLORS[slot][0])
        return slot

    def slot_for(self, label: Label) -> str | None:
        key = label.name.lower()
        if self.config.mode == "off" or key in self._exclude:
            return None
        target = self._mapping.get(key)
        if target is None:
            if self.config.mode != "auto":
                return None
            target = label.name
        if is_category_slot(target):
            return target
        slot = self._slot_by_name(target)
        if slot is None and self.config.create_missing:
            slot = self._allocate(target, label.color)
        elif slot is None and key not in self._warned:
            self._warned.add(key)
            log.warning("Planner category %r for label %r does not exist", target, label.name)
        return slot

    def resolve(self, labels: Iterable[Label]) -> list[str]:
        slots = {slot for label in labels if (slot := self.slot_for(label))}
        return sorted(slots, key=lambda s: int(s.removeprefix("category")))

    def name_of(self, slot: str) -> str:
        return self.pending.get(slot) or self.descriptions.get(slot) or slot

    def label_for(self, slot: str) -> str | None:
        """The label a Planner category stands for (reverse mapping), if any."""
        if self.config.mode == "off" or slot not in CATEGORY_SLOTS:
            return None
        description = self.pending.get(slot) or self.descriptions.get(slot)
        for label, target in self.config.mapping.items():
            if target == slot or (
                description and target.strip().lower() == description.strip().lower()
            ):
                return None if label.lower() in self._exclude else label
        if self.config.mode == "auto" and description:
            if description.lower() in self._exclude or description.lower() in self._mapping:
                return None
            return description
        return None

    def color_of(self, slot: str) -> str:
        return PLANNER_CATEGORY_COLORS[slot][1]


# --------------------------------------------------------------------------- buckets


def bucket_name_for(issue: Issue, config: BucketConfig) -> str:
    """Pick the bucket *name* an issue belongs to."""
    if not issue.is_open and config.closed:
        return config.closed
    if config.strategy == "label":
        lowered = {name.lower() for name in issue.label_names}
        for label, bucket in config.mapping.items():
            if label.lower() in lowered:
                return bucket
    elif config.strategy == "milestone" and issue.milestone is not None:
        title = issue.milestone.title
        for milestone, bucket in config.mapping.items():
            if milestone.lower() == title.lower():
                return bucket
        return title
    return config.default


def bucket_label(config: BucketConfig, bucket_name: str) -> str | None:
    """The label that puts an issue into ``bucket_name`` (``label`` strategy only)."""
    if config.strategy != "label":
        return None
    for label, bucket in config.mapping.items():
        if bucket.strip().lower() == bucket_name.strip().lower():
            return label
    return None


# --------------------------------------------------------------------------- priority


def priority_for(issue: Issue, config: PriorityConfig) -> int:
    """The most urgent priority among the issue's labels, else the default."""
    lowered = {name.lower(): name for name in issue.label_names}
    values = [value for label, value in config.mapping.items() if label.lower() in lowered]
    return min(values) if values else config.default


def priority_band(value: int) -> int:
    """Planner groups priorities 0-10 into urgent, important, medium and low."""
    if value <= 1:
        return 0
    if value <= 4:
        return 1
    if value <= 7:
        return 2
    return 3


def priority_label(config: PriorityConfig, value: int) -> str | None:
    """The configured label matching a Planner priority (reverse mapping)."""
    for label, mapped in config.mapping.items():
        if priority_band(mapped) == priority_band(value):
            return label
    return None


# --------------------------------------------------------------------------- assignees


class DirectoryAPI(Protocol):
    def get_user(self, identifier: str) -> dict[str, Any] | None: ...

    def find_user_by_email(self, email: str) -> dict[str, Any] | None: ...

    def list_group_member_ids(self, group_id: str) -> set[str]: ...


class UserLookup(Protocol):
    def get_user(self, login: str) -> GitUser: ...


@dataclass
class AssigneeResolver:
    """Resolve Git users to Microsoft Entra ID object ids."""

    config: AssigneeConfig
    directory: DirectoryAPI
    users: UserLookup
    group_id: str | None = None
    _cache: dict[str, str | None] = field(default_factory=dict)
    _members: set[str] | None = None
    _membership_checked: bool = False
    _warned: set[str] = field(default_factory=set)
    _directory_denied: bool = False
    _logins: dict[str, str] = field(default_factory=dict)
    _lookups: dict[str, str | None] = field(default_factory=dict)

    def _members_of_group(self) -> set[str] | None:
        if self._membership_checked:
            return self._members
        self._membership_checked = True
        if not self.config.require_group_membership or not self.group_id:
            return None
        try:
            self._members = self.directory.list_group_member_ids(self.group_id)
        except PermissionDeniedError as exc:
            log.warning(
                "cannot read members of group %s (%s); group membership will not be verified",
                self.group_id,
                exc,
            )
            self._members = None
        return self._members

    def _lookup(self, identifier: str) -> str | None:
        """Resolve an object id, UPN or e-mail address to an object id."""
        identifier = identifier.strip()
        if not identifier:
            return None
        if _GUID.match(identifier):
            return identifier.lower()
        key = identifier.lower()
        if key not in self._lookups:
            self._lookups[key] = self._directory_lookup(identifier)
        return self._lookups[key]

    def _directory_lookup(self, identifier: str) -> str | None:
        if self._directory_denied:
            return None
        try:
            user = self.directory.get_user(identifier)
            if user is None and "@" in identifier:
                user = self.directory.find_user_by_email(identifier)
        except PermissionDeniedError as exc:
            self._directory_denied = True
            log.error(
                "cannot read users from Microsoft Entra ID (%s); grant User.Read.All "
                "(application) or User.ReadBasic.All (delegated), or map assignees to object ids",
                exc,
            )
            return None
        except HTTPError as exc:
            log.debug("directory lookup of %s failed: %s", identifier, exc)
            return None
        return str(user["id"]).lower() if user and user.get("id") else None

    def _candidates(self, user: GitUser) -> list[str]:
        candidates: list[str] = []
        mapped = {key.lower(): value for key, value in self.config.mapping.items()}
        if user.login.lower() in mapped:
            candidates.append(mapped[user.login.lower()])
        if self.config.use_git_email:
            email = user.email
            if not email:
                email = self.users.get_user(user.login).email
            if email and "noreply" not in email.lower():
                candidates.append(email)
        if self.config.email_domain:
            candidates.append(f"{user.login}@{self.config.email_domain}")
        return candidates

    def resolve_user(self, user: GitUser) -> str | None:
        key = user.login.lower()
        if key in self._cache:
            return self._cache[key]
        object_id: str | None = None
        for candidate in self._candidates(user):
            object_id = self._lookup(candidate)
            if object_id:
                break
        if object_id is None:
            self._warn(key, f"cannot map Git user {user.login!r} to a Microsoft 365 user")
        else:
            members = self._members_of_group()
            if members is not None and object_id not in {m.lower() for m in members}:
                self._warn(
                    key,
                    f"Microsoft 365 user for {user.login!r} is not a member of the plan's group",
                )
                object_id = None
        self._cache[key] = object_id
        if object_id and not self._logins.get(object_id):
            self._logins[object_id] = user.login
        return object_id

    def login_for(self, object_id: str) -> str | None:
        """Map a Microsoft Entra object id back to a Git login."""
        object_id = object_id.lower()
        if object_id in self._logins:
            return self._logins[object_id] or None
        login = self._reverse_lookup(object_id)
        self._logins[object_id] = login or ""
        return login

    def _reverse_lookup(self, object_id: str) -> str | None:
        for login, target in self.config.mapping.items():
            if self._lookup(target) == object_id:
                return login
        if self.config.email_domain and not self._directory_denied:
            try:
                user = self.directory.get_user(object_id)
            except HTTPError as exc:
                log.debug("directory lookup of %s failed: %s", object_id, exc)
                user = None
            suffix = f"@{self.config.email_domain.lower()}"
            addresses = [(user or {}).get("userPrincipalName"), (user or {}).get("mail")]
            for address in addresses:
                if isinstance(address, str) and address.lower().endswith(suffix):
                    return address[: -len(suffix)]
        return None

    def resolve_identifier(self, identifier: str) -> str | None:
        key = f"id:{identifier.lower()}"
        if key not in self._cache:
            object_id = self._lookup(identifier)
            if object_id is None:
                self._warn(key, f"fallback assignee {identifier!r} not found")
            self._cache[key] = object_id
        return self._cache[key]

    def resolve(self, users: Iterable[GitUser]) -> list[str]:
        if not self.config.enabled:
            return []
        ids: list[str] = []
        for user in users:
            object_id = self.resolve_user(user)
            if object_id and object_id not in ids:
                ids.append(object_id)
        if not ids:
            for identifier in self.config.fallback:
                object_id = self.resolve_identifier(identifier)
                if object_id and object_id not in ids:
                    ids.append(object_id)
        return ids

    def _warn(self, key: str, message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(message)
