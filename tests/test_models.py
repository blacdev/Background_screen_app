from __future__ import annotations

from models import (
    DAY_OPTIONS,
    ScheduleEntry,
    ScheduleMediaAssignment,
    format_day_selection,
    normalize_day_key,
    normalized_day_keys,
)


def test_normalize_day_key_accepts_full_names_and_abbreviations() -> None:
    assert normalize_day_key("Mon") == "mon"
    assert normalize_day_key("monday") == "mon"
    assert normalize_day_key("Friday") == "fri"
    assert normalize_day_key("not-a-day") is None


def test_normalized_day_keys_defaults_to_every_day_when_empty() -> None:
    assert normalized_day_keys([]) == [key for key, _label in DAY_OPTIONS]


def test_format_day_selection_returns_every_day_for_default_selection() -> None:
    assert format_day_selection([]) == "Every day"


def test_schedule_media_assignment_from_dict_normalizes_payload() -> None:
    assignment = ScheduleMediaAssignment.from_dict(
        {
            "screen_id": " screen-a ",
            "media_files": ["a.jpg", "a.jpg", "folder\\b.jpg", "", 1],
            "mode": "CYCLE",
            "interval_seconds": 1,
        }
    )
    assert assignment.screen_id == "screen-a"
    assert assignment.media_files == ["a.jpg", "folder/b.jpg"]
    assert assignment.mode == "cycle"
    assert assignment.interval_seconds == 2


def test_schedule_entry_from_dict_discards_group_media_assignment_keys() -> None:
    entry = ScheduleEntry.from_dict(
        {
            "id": "sched-1",
            "title": "Morning",
            "start_time": "09:00",
            "end_time": "10:00",
            "video_file": "demo.mp4",
            "video_label": "Demo",
            "screen_ids": ["group:default"],
            "days": ["monday"],
            "media_assignments": {
                "group:default": {
                    "screen_id": "group:default",
                    "media_files": ["demo.jpg"],
                },
                "screen-a": {
                    "screen_id": "screen-a",
                    "media_files": ["screen-a.jpg"],
                },
            },
        }
    )
    assert entry.days == ["mon"]
    assert list(entry.media_assignments) == ["screen-a"]


def test_schedule_entry_weekly_segments_handles_overnight_ranges() -> None:
    entry = ScheduleEntry(
        id="sched-1",
        title="Overnight",
        start_time="23:00",
        end_time="01:00",
        video_file="demo.mp4",
        video_label="Demo",
        screen_ids=["screen-a"],
        days=["mon"],
        media_assignments={},
    )
    assert entry.weekly_segments() == [(0, 1380, 1440), (1, 0, 60)]
