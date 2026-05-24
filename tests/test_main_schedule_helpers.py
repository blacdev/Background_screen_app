from __future__ import annotations

from types import SimpleNamespace

import main as main_module
import pytest
from main import (
    DEFAULT_GROUP_ID,
    ControllerEngine,
    MainWindow,
    PlaybackCoordinator,
    disconnected_screen_ids,
    format_screen_targets,
)
from models import ScheduleEntry, ScheduleMediaAssignment
from scheduling import (
    active_schedule_for_screen,
    schedule_assignment_for_screen,
    validate_schedule_media_assignments,
)
from screen_groups import ScreenGroup, combined_screen_groups
from screen_protocols import ConfiguredScreen


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
        [
            ScreenGroup.from_dict(
                {
                    "id": "group:lobby",
                    "name": "Lobby",
                    "screen_ids": ["screen-2", "screen-3"],
                }
            )
        ],
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
    assert (
        active_schedule_for_screen([entry], 0, (9 * 60) + 15, "screen-9", groups)
        is None
    )


def test_format_screen_targets_uses_group_names() -> None:
    groups = combined_screen_groups(
        ["screen-1"],
        [
            ScreenGroup.from_dict(
                {"id": "group:lobby", "name": "Lobby", "screen_ids": ["screen-2"]}
            )
        ],
    )
    label = format_screen_targets(["group:lobby", DEFAULT_GROUP_ID], {}, groups)
    assert label == "Lobby, Default Playback Group"


def test_disconnected_screen_ids_ignores_group_target_ids() -> None:
    groups = combined_screen_groups(["screen-1"], [])
    result = disconnected_screen_ids([DEFAULT_GROUP_ID, "screen:missing"], groups)
    assert result == ["screen:missing"]


def test_main_window_static_media_configured_screen_ids_include_only_media_capable_targets() -> (
    None
):
    window_like = SimpleNamespace(
        configured_screens=[
            ConfiguredScreen(
                id="browser-1",
                name="Phone",
                transport="browser",
                capabilities=["static_media", "playlist"],
            ),
            ConfiguredScreen(
                id="local-1",
                name="Control Monitor",
                transport="local_hdmi",
                capabilities=["live_view", "sync_playback"],
            ),
        ]
    )
    assert MainWindow.static_media_configured_screen_ids(window_like) == {"browser-1"}


def test_main_window_browser_configured_screen_helper_filters_by_transport() -> None:
    window_like = SimpleNamespace(
        configured_screens=[
            ConfiguredScreen(
                id="browser-1",
                name="Phone",
                transport="browser",
                capabilities=["static_media"],
            ),
            ConfiguredScreen(
                id="local-1",
                name="Laptop HDMI",
                transport="local_hdmi",
                capabilities=["static_media", "playlist", "live_view", "sync_playback"],
            ),
        ]
    )
    assert MainWindow.browser_configured_screen_ids(window_like) == {"browser-1"}


def test_schedule_entry_media_assignments_round_trip() -> None:
    entry = ScheduleEntry.from_dict(
        {
            "id": "sched-1",
            "title": "Afternoon",
            "start_time": "14:00",
            "end_time": "15:00",
            "video_file": "default.mp4",
            "video_label": "default.mp4",
            "screen_ids": ["screen-a"],
            "days": ["mon"],
            "media_assignments": {
                "screen-a": {
                    "screen_id": "screen-a",
                    "media_files": ["a-1.jpg", "a-2.jpg"],
                    "mode": "cycle",
                    "interval_seconds": 7,
                }
            },
        }
    )
    assert "screen-a" in entry.media_assignments
    assert entry.media_assignments["screen-a"].mode == "cycle"
    payload = entry.to_dict()
    assert payload["media_assignments"]["screen-a"]["media_files"] == [
        "a-1.jpg",
        "a-2.jpg",
    ]


def test_schedule_assignment_for_screen_prefers_specific_assignment() -> None:
    entry = ScheduleEntry.from_dict(
        {
            "id": "sched-1",
            "title": "Afternoon",
            "start_time": "14:00",
            "end_time": "15:00",
            "video_file": "default.mp4",
            "video_label": "default.mp4",
            "screen_ids": ["screen-a"],
            "days": ["mon"],
            "media_assignments": {
                "screen-a": {
                    "screen_id": "screen-a",
                    "media_files": ["a-1.jpg", "a-2.jpg"],
                    "mode": "cycle",
                    "interval_seconds": 7,
                }
            },
        }
    )
    assignment = schedule_assignment_for_screen(entry, "screen-a")
    assert assignment is not None
    assert assignment.media_files == ["a-1.jpg", "a-2.jpg"]


def test_validate_schedule_media_assignments_rejects_non_image_cycle(tmp_path) -> None:
    (tmp_path / "a.mp4").write_bytes(b"x")
    entry = ScheduleEntry(
        id="sched-1",
        title="Afternoon",
        start_time="14:00",
        end_time="15:00",
        video_file="default.mp4",
        video_label="default.mp4",
        screen_ids=["screen-a"],
        days=["mon"],
        media_assignments={
            "screen-a": ScheduleMediaAssignment(
                screen_id="screen-a",
                media_files=["a.mp4", "a.mp4"],
                mode="cycle",
                interval_seconds=10,
            )
        },
    )
    with pytest.raises(ValueError, match="Image cycling supports image files only"):
        validate_schedule_media_assignments(entry, tmp_path)


def test_playback_coordinator_assignment_for_screen_uses_cycle_metadata(
    tmp_path,
) -> None:
    (tmp_path / "default.mp4").write_bytes(b"x")
    (tmp_path / "a-1.jpg").write_bytes(b"x")
    (tmp_path / "a-2.jpg").write_bytes(b"x")
    coordinator = PlaybackCoordinator()
    coordinator.manage_local_windows = False
    coordinator.video_directory = tmp_path
    coordinator.screen_groups = []
    entry = ScheduleEntry.from_dict(
        {
            "id": "sched-1",
            "title": "Afternoon",
            "start_time": "14:00",
            "end_time": "15:00",
            "video_file": "default.mp4",
            "video_label": "default.mp4",
            "screen_ids": ["screen-a"],
            "days": ["mon"],
            "media_assignments": {
                "screen-a": {
                    "screen_id": "screen-a",
                    "media_files": ["a-1.jpg", "a-2.jpg"],
                    "mode": "cycle",
                    "interval_seconds": 9,
                }
            },
        }
    )
    coordinator.set_schedules([entry])
    assignment = coordinator.assignment_for_screen("screen-a", 0, (14 * 60) + 10)
    assert assignment["cycle"] is True
    assert len(assignment["paths"]) == 2
    command = coordinator.build_screen_command("screen-a", assignment, 123, 1)
    assert command.cycle is True
    assert len(command.paths) == 2


def test_controller_engine_unified_screen_targets_uses_static_media_filter(
    monkeypatch,
) -> None:
    monkeypatch.setattr(main_module, "available_screens", lambda: [])
    engine_like = SimpleNamespace(
        configured_screens=[
            ConfiguredScreen(
                id="browser-1",
                name="Phone",
                transport="browser",
                capabilities=["static_media"],
            ),
            ConfiguredScreen(
                id="local-1",
                name="Wall Screen",
                transport="local_hdmi",
                capabilities=["static_media", "playlist", "live_view", "sync_playback"],
            ),
        ],
        screen_aliases={},
        remote_screens={},
        static_media_configured_screen_ids=lambda: {"browser-1", "local-1"},
        browser_configured_screens=lambda: [
            ConfiguredScreen(
                id="browser-1",
                name="Phone",
                transport="browser",
                capabilities=["static_media"],
            )
        ],
    )
    targets = ControllerEngine.unified_screen_targets(engine_like)
    assert [target.id for target in targets] == ["browser-1"]
