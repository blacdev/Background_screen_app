from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Iterable, Mapping


GROUP_PREFIX = "group:"
DEFAULT_GROUP_ID = "group:default"
DEFAULT_GROUP_NAME = "Default Playback Group"


def is_group_target_id(value: object) -> bool:
    return str(value or "").strip().startswith(GROUP_PREFIX)


@dataclass
class ScreenGroup:
    id: str
    name: str
    screen_ids: list[str]

    def validate(self) -> None:
        if not self.id.strip().startswith(GROUP_PREFIX):
            raise ValueError("Screen group ids must start with group:.")
        if not self.name.strip():
            raise ValueError("Screen groups must have a name.")
        normalized: list[str] = []
        seen: set[str] = set()
        for screen_id in self.screen_ids:
            cleaned = str(screen_id).strip()
            if not cleaned or is_group_target_id(cleaned) or cleaned in seen:
                continue
            seen.add(cleaned)
            normalized.append(cleaned)
        self.screen_ids = normalized

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "id": self.id,
            "name": self.name,
            "screen_ids": self.screen_ids[:],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ScreenGroup":
        group_id = str(data.get("id") or "").strip()
        if not group_id:
            group_id = f"{GROUP_PREFIX}{uuid.uuid4().hex}"
        group = cls(
            id=group_id,
            name=str(data.get("name") or "").strip(),
            screen_ids=[str(item) for item in data.get("screen_ids", []) if isinstance(item, str)],
        )
        group.validate()
        return group


def default_screen_group(screen_ids: Iterable[str]) -> ScreenGroup:
    group = ScreenGroup(
        id=DEFAULT_GROUP_ID,
        name=DEFAULT_GROUP_NAME,
        screen_ids=[str(item) for item in screen_ids if str(item).strip()],
    )
    group.validate()
    return group


def parse_screen_groups(raw_items: object) -> list[ScreenGroup]:
    if not isinstance(raw_items, list):
        return []
    groups: list[ScreenGroup] = []
    seen_ids: set[str] = set()
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        try:
            group = ScreenGroup.from_dict(item)
        except ValueError:
            continue
        if group.id == DEFAULT_GROUP_ID or group.id in seen_ids:
            continue
        seen_ids.add(group.id)
        groups.append(group)
    return groups


def validate_screen_groups(raw_items: object) -> list[ScreenGroup]:
    if not isinstance(raw_items, list):
        raise ValueError("Screen groups payload must be a list.")
    groups: list[ScreenGroup] = []
    seen_ids: set[str] = set()
    for item in raw_items:
        if not isinstance(item, Mapping):
            raise ValueError("Each screen group must be an object.")
        group = ScreenGroup.from_dict(item)
        if group.id == DEFAULT_GROUP_ID:
            raise ValueError("The default playback group is managed by the app.")
        if group.id in seen_ids:
            raise ValueError("Screen group ids must be unique.")
        seen_ids.add(group.id)
        groups.append(group)
    return groups


def combined_screen_groups(selected_screen_ids: Iterable[str], extra_groups: list[ScreenGroup]) -> list[ScreenGroup]:
    groups = [default_screen_group(selected_screen_ids)]
    groups.extend(ScreenGroup.from_dict(group.to_dict()) for group in extra_groups)
    return groups


def normalize_schedule_target_ids(target_ids: Iterable[str] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for target_id in target_ids or []:
        cleaned = str(target_id).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        normalized.append(cleaned)
    return normalized


def expand_target_ids(target_ids: Iterable[str] | None, groups: list[ScreenGroup]) -> list[str]:
    normalized = normalize_schedule_target_ids(target_ids)
    group_map = {group.id: group for group in groups}
    expanded: list[str] = []
    seen: set[str] = set()
    for target_id in normalized:
        if is_group_target_id(target_id):
            group = group_map.get(target_id)
            if group is None:
                continue
            members = group.screen_ids
        else:
            members = [target_id]
        for member in members:
            if member in seen:
                continue
            seen.add(member)
            expanded.append(member)
    return expanded


def target_contains_screen(target_ids: Iterable[str] | None, screen_id: str, groups: list[ScreenGroup]) -> bool:
    return screen_id in set(expand_target_ids(target_ids, groups))


def target_label_map(groups: list[ScreenGroup]) -> dict[str, str]:
    return {group.id: group.name for group in groups}
