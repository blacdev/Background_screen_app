from __future__ import annotations

from datetime import datetime

import pytest
from models import ScheduleEntry, ScheduleMediaAssignment
from scheduling import (
    active_schedule_for_minute,
    schedule_progress,
    validate_schedule_candidate,
)


def test_active_schedule_for_minute_returns_matching_entry() -> None:
    entry = ScheduleEntry(
        id="sched-1",
        title="Morning",
        start_time="09:00",
        end_time="10:00",
        video_file="demo.mp4",
        video_label="Demo",
        screen_ids=["screen-a"],
        days=["mon"],
        media_assignments={},
    )
    match = active_schedule_for_minute([entry], 0, 9 * 60 + 30)
    assert match is not None
    assert match.id == "sched-1"


def test_schedule_progress_handles_overnight() -> None:
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
    value = schedule_progress(entry, datetime(2026, 5, 24, 0, 0, 0))
    assert 0.49 <= value <= 0.51


def test_validate_schedule_candidate_rejects_overlap() -> None:
    existing = ScheduleEntry(
        id="sched-existing",
        title="Existing",
        start_time="09:00",
        end_time="10:00",
        video_file="one.mp4",
        video_label="One",
        screen_ids=["screen-a"],
        days=["mon"],
        media_assignments={},
    )
    candidate = ScheduleEntry(
        id="sched-candidate",
        title="Candidate",
        start_time="09:30",
        end_time="10:30",
        video_file="two.mp4",
        video_label="Two",
        screen_ids=["screen-a"],
        days=["mon"],
        media_assignments={
            "screen-a": ScheduleMediaAssignment(
                screen_id="screen-a",
                media_files=["image.jpg"],
                mode="single",
                interval_seconds=10,
            )
        },
    )

    with pytest.raises(ValueError, match="overlaps"):
        validate_schedule_candidate(candidate, [existing], video_directory=None)
