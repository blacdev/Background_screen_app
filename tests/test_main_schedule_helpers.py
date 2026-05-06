from __future__ import annotations

from main import (
    DEFAULT_GROUP_ID,
    ScheduleEntry,
    active_schedule_for_screen,
    disconnected_screen_ids,
    format_screen_targets,
)
from screen_groups import ScreenGroup, combined_screen_groups


def test_schedule_entry_defaults_to_default_group_when_no_targets_are_saved() -> None:
    entry = ScheduleEntry.from_dict(
        {
            "id": "sched-1",
            "title": "Morning",
            "start_time": "09:00",
            "end_time": "10:00",
            "video_file": "demo.mp4",
            "video_label": "Demo",
            "screen_ids": [],
            "days": ["mon"],
        }
    )
    assert entry.screen_ids == [DEFAULT_GROUP_ID]


def test_active_schedule_for_screen_expands_group_targets() -> None:
    groups = combined_screen_groups(
        ["screen-1"],
        [ScreenGroup.from_dict({"id": "group:lobby", "name": "Lobby", "screen_ids": ["screen-2", "screen-3"]})],
    )
    entry = ScheduleEntry.from_dict(
        {
            "id": "sched-1",
            "title": "Morning",
            "start_time": "09:00",
            "end_time": "10:00",
            "video_file": "demo.mp4",
            "video_label": "Demo",
            "screen_ids": ["group:lobby"],
            "days": ["mon"],
        }
    )
    match = active_schedule_for_screen([entry], 0, (9 * 60) + 15, "screen-2", groups)
    assert match is not None
    assert match.id == "sched-1"
    assert active_schedule_for_screen([entry], 0, (9 * 60) + 15, "screen-9", groups) is None


def test_format_screen_targets_uses_group_names() -> None:
    groups = combined_screen_groups(
        ["screen-1"],
        [ScreenGroup.from_dict({"id": "group:lobby", "name": "Lobby", "screen_ids": ["screen-2"]})],
    )
    label = format_screen_targets(["group:lobby", DEFAULT_GROUP_ID], {}, groups)
    assert label == "Lobby, Default Playback Group"


def test_disconnected_screen_ids_ignores_group_target_ids() -> None:
    groups = combined_screen_groups(["screen-1"], [])
    result = disconnected_screen_ids([DEFAULT_GROUP_ID, "screen:missing"], groups)
    assert result == ["screen:missing"]
