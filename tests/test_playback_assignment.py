from __future__ import annotations

from datetime import datetime
from pathlib import Path

from models import ScheduleEntry
from playback_assignment import PlaybackAssignmentService


def test_assignment_for_screen_returns_quick_play_override(tmp_path: Path) -> None:
    media = tmp_path / "override.jpg"
    media.write_bytes(b"x")
    service = PlaybackAssignmentService()

    assignment = service.assignment_for_screen(
        screen_id="screen-a",
        weekday_index=0,
        minute_of_day=600,
        quick_play_paths={"screen-a": media},
        quick_play_labels={"screen-a": "Override"},
        is_paused=False,
        schedules=[],
        screen_groups=[],
        video_directory=tmp_path,
    )

    assert assignment["type"] == "play"
    assert assignment["mode"] == "quick_play"
    assert assignment["label"] == "Override"


def test_assignment_for_screen_returns_cycle_metadata(tmp_path: Path) -> None:
    (tmp_path / "a-1.jpg").write_bytes(b"x")
    (tmp_path / "a-2.jpg").write_bytes(b"x")
    entry = ScheduleEntry.from_dict(
        {
            "id": "sched-1",
            "title": "Afternoon",
            "start_time": "14:00",
            "end_time": "15:00",
            "video_file": "default.jpg",
            "video_label": "default.jpg",
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
    service = PlaybackAssignmentService()

    assignment = service.assignment_for_screen(
        screen_id="screen-a",
        weekday_index=0,
        minute_of_day=(14 * 60) + 10,
        quick_play_paths={},
        quick_play_labels={},
        is_paused=False,
        schedules=[entry],
        screen_groups=[],
        video_directory=tmp_path,
    )

    assert assignment["type"] == "play"
    assert assignment["cycle"] is True
    assert len(assignment["paths"]) == 2


def test_build_screen_command_sets_media_kind(tmp_path: Path) -> None:
    media = tmp_path / "demo.jpg"
    media.write_bytes(b"x")
    service = PlaybackAssignmentService()
    assignment = {
        "type": "play",
        "mode": "schedule",
        "path": media,
        "paths": [media],
        "relative_path": "demo.jpg",
        "relative_paths": ["demo.jpg"],
        "entry_id": "sched-1",
        "message": "",
        "label": "Demo",
        "cycle": False,
        "cycle_interval_seconds": 10,
    }

    command = service.build_screen_command(
        screen_id="screen-a",
        assignment=assignment,
        play_at_ms=123,
        version=1,
        transition_method="fade_black",
        is_paused=False,
    )

    assert command.media_kind == "image"
    assert command.paths == [str(media)]


def test_next_schedule_change_delay_ms_returns_future_delay() -> None:
    entry = ScheduleEntry.from_dict(
        {
            "id": "sched-1",
            "title": "Morning",
            "start_time": "09:00",
            "end_time": "10:00",
            "video_file": "demo.jpg",
            "video_label": "demo.jpg",
            "screen_ids": ["screen-a"],
            "days": ["mon"],
        }
    )
    service = PlaybackAssignmentService()
    now = datetime(2026, 5, 25, 8, 0, 0).replace(
        tzinfo=datetime.now().astimezone().tzinfo
    )
    delay = service.next_schedule_change_delay_ms(
        schedules=[entry],
        now=now,
        tz=now.tzinfo,
    )

    assert delay is not None
    assert delay > 0
