from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from screen_groups import (
    DEFAULT_GROUP_ID,
    is_group_target_id,
    normalize_schedule_target_ids,
)

DAY_OPTIONS = [
    ("mon", "Mon"),
    ("tue", "Tue"),
    ("wed", "Wed"),
    ("thu", "Thu"),
    ("fri", "Fri"),
    ("sat", "Sat"),
    ("sun", "Sun"),
]
DAY_KEYS = [key for key, _label in DAY_OPTIONS]
DAY_LABELS = dict(DAY_OPTIONS)
DAY_KEY_TO_INDEX = {key: index for index, key in enumerate(DAY_KEYS)}
DAY_INDEX_TO_KEY = {index: key for key, index in DAY_KEY_TO_INDEX.items()}


def normalize_day_key(value: str) -> str | None:
    cleaned = value.strip().lower()
    for key in DAY_KEYS:
        if cleaned == key or cleaned.startswith(key):
            return key
    return None


def normalized_day_keys(days: list[str] | tuple[str, ...] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in days or []:
        key = normalize_day_key(str(item))
        if key is None or key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    return normalized if normalized else DAY_KEYS[:]


def format_day_selection(days: list[str] | tuple[str, ...] | None) -> str:
    normalized = normalized_day_keys(list(days or []))
    if normalized == DAY_KEYS:
        return "Every day"
    return ", ".join(DAY_LABELS[key] for key in normalized)


@dataclass(frozen=True, slots=True)
class ScreenId:
    value: str


@dataclass(slots=True)
class ScreenRecord:
    id: str
    display_name: str
    transport: Literal["local_hdmi", "browser"]
    online: bool
    enabled: bool
    source: Literal["detected", "remembered", "configured"]
    metadata: dict[str, Any]


@dataclass
class ScheduleMediaAssignment:
    screen_id: str
    media_files: list[str]
    mode: str = "single"
    interval_seconds: int = 10

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ScheduleMediaAssignment":
        screen_id = str(data.get("screen_id") or "").strip()
        files: list[str] = []
        for item in data.get("media_files", []):
            if not isinstance(item, str):
                continue
            value = item.strip().replace("\\", "/")
            if value:
                files.append(value)
        mode = str(data.get("mode") or "single").strip().lower()
        if mode not in {"single", "cycle"}:
            mode = "single"
        try:
            interval = int(data.get("interval_seconds") or 10)
        except (TypeError, ValueError):
            interval = 10
        interval = max(2, min(interval, 3600))
        return cls(
            screen_id=screen_id,
            media_files=list(dict.fromkeys(files)),
            mode=mode,
            interval_seconds=interval,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "screen_id": self.screen_id,
            "media_files": self.media_files,
            "mode": self.mode,
            "interval_seconds": self.interval_seconds,
        }


@dataclass
class UnifiedScreenTarget:
    id: str
    label: str
    kind: str
    online: bool
    detail: str = ""
    warning: str = ""


@dataclass
class ScheduleEntry:
    id: str
    title: str
    start_time: str
    end_time: str
    video_file: str
    video_label: str
    screen_ids: list[str]
    days: list[str]
    media_assignments: dict[str, ScheduleMediaAssignment]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ScheduleEntry":
        raw_screen_ids = [
            str(item) for item in data.get("screen_ids", []) if isinstance(item, str)
        ]
        normalized_screen_ids = normalize_schedule_target_ids(raw_screen_ids)
        if not normalized_screen_ids:
            normalized_screen_ids = [DEFAULT_GROUP_ID]
        raw_assignments = data.get("media_assignments")
        parsed_assignments: dict[str, ScheduleMediaAssignment] = {}
        if isinstance(raw_assignments, Mapping):
            for assignment_key, assignment_value in raw_assignments.items():
                if not isinstance(assignment_value, Mapping):
                    continue
                assignment = ScheduleMediaAssignment.from_dict(assignment_value)
                key = assignment.screen_id or str(assignment_key or "").strip()
                if not key or is_group_target_id(key) or not assignment.media_files:
                    continue
                assignment.screen_id = key
                parsed_assignments[key] = assignment
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            title=str(data.get("title") or "Untitled schedule"),
            start_time=str(data.get("start_time") or "00:00"),
            end_time=str(data.get("end_time") or "00:00"),
            video_file=str(data.get("video_file") or ""),
            video_label=str(data.get("video_label") or data.get("video_file") or ""),
            screen_ids=normalized_screen_ids,
            days=[
                key
                for item in data.get("days", [])
                if isinstance(item, str)
                if (key := normalize_day_key(item)) is not None
            ],
            media_assignments=parsed_assignments,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "video_file": self.video_file,
            "video_label": self.video_label,
            "screen_ids": self.screen_ids,
            "days": self.days,
            "media_assignments": {
                screen_id: assignment.to_dict()
                for screen_id, assignment in self.media_assignments.items()
                if assignment.media_files
            },
        }

    @property
    def range_label(self) -> str:
        return f"{self.start_time} - {self.end_time} UK"

    @property
    def days_label(self) -> str:
        return format_day_selection(self.days)

    @staticmethod
    def time_to_minutes(value: str) -> int:
        hours, minutes = [int(part) for part in value.split(":")]
        return (hours * 60) + minutes

    def selected_days(self) -> list[str]:
        return normalized_day_keys(self.days)

    def covers_minute(self, minute_of_day: int) -> bool:
        start = self.time_to_minutes(self.start_time)
        end = self.time_to_minutes(self.end_time)
        if start < end:
            return start <= minute_of_day < end
        return minute_of_day >= start or minute_of_day < end

    def covers_weekday_minute(self, weekday_index: int, minute_of_day: int) -> bool:
        if weekday_index not in DAY_INDEX_TO_KEY:
            return False
        start = self.time_to_minutes(self.start_time)
        end = self.time_to_minutes(self.end_time)
        current_day_key = DAY_INDEX_TO_KEY[weekday_index]
        if start < end:
            return (
                current_day_key in self.selected_days() and start <= minute_of_day < end
            )
        if minute_of_day >= start:
            return current_day_key in self.selected_days()
        previous_day_key = DAY_INDEX_TO_KEY[(weekday_index - 1) % 7]
        return previous_day_key in self.selected_days() and minute_of_day < end

    def split_ranges(self) -> list[tuple[int, int]]:
        start = self.time_to_minutes(self.start_time)
        end = self.time_to_minutes(self.end_time)
        if start == end:
            return []
        if start < end:
            return [(start, end)]
        return [(start, 1440), (0, end)]

    def weekly_segments(self) -> list[tuple[int, int, int]]:
        start = self.time_to_minutes(self.start_time)
        end = self.time_to_minutes(self.end_time)
        segments: list[tuple[int, int, int]] = []
        if start == end:
            return segments
        for day_key in self.selected_days():
            day_index = DAY_KEY_TO_INDEX[day_key]
            if start < end:
                segments.append((day_index, start, end))
                continue
            segments.append((day_index, start, 1440))
            segments.append(((day_index + 1) % 7, 0, end))
        return segments
