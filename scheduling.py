from __future__ import annotations

from datetime import datetime
from pathlib import Path

from models import ScheduleEntry, ScheduleMediaAssignment
from screen_groups import ScreenGroup, is_group_target_id, target_contains_screen

SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".ogg"}
SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
SUPPORTED_MEDIA_EXTENSIONS = SUPPORTED_VIDEO_EXTENSIONS | SUPPORTED_IMAGE_EXTENSIONS


def active_schedule_for_minute(
    schedules: list[ScheduleEntry], weekday_index: int, minute_of_day: int
) -> ScheduleEntry | None:
    for entry in sorted(
        schedules, key=lambda item: ScheduleEntry.time_to_minutes(item.start_time)
    ):
        if entry.covers_weekday_minute(weekday_index, minute_of_day):
            return entry
    return None


def schedule_progress(entry: ScheduleEntry, now: datetime) -> float:
    current_minutes = (now.hour * 60) + now.minute + (now.second / 60.0)
    start = ScheduleEntry.time_to_minutes(entry.start_time)
    end = ScheduleEntry.time_to_minutes(entry.end_time)
    if start < end:
        total = max(end - start, 1)
        progressed = min(max(current_minutes - start, 0.0), float(total))
        return progressed / total
    total = max((1440 - start) + end, 1)
    if current_minutes >= start:
        progressed = current_minutes - start
    else:
        progressed = (1440 - start) + current_minutes
    progressed = min(max(progressed, 0.0), float(total))
    return progressed / total


def schedule_targets_overlap(first: list[str], second: list[str]) -> bool:
    if not first or not second:
        return True
    return bool(set(first) & set(second))


def schedule_assignment_for_screen(
    entry: ScheduleEntry, screen_id: str
) -> ScheduleMediaAssignment | None:
    assignment = entry.media_assignments.get(screen_id)
    if assignment is not None and assignment.media_files:
        return assignment
    return None


def validate_schedule_media_assignments(
    candidate: ScheduleEntry,
    video_directory: Path | None,
    *,
    supported_media_extensions: set[str] = SUPPORTED_MEDIA_EXTENSIONS,
    supported_image_extensions: set[str] = SUPPORTED_IMAGE_EXTENSIONS,
) -> None:
    for screen_id, assignment in candidate.media_assignments.items():
        if not screen_id or is_group_target_id(screen_id):
            raise ValueError(
                "Per-screen assignments must target concrete screens, not groups."
            )
        if assignment.mode not in {"single", "cycle"}:
            raise ValueError("Per-screen assignment mode must be single or cycle.")
        if not assignment.media_files:
            raise ValueError(
                "Per-screen assignments must include at least one media file."
            )
        if len(assignment.media_files) > 1 and assignment.mode != "cycle":
            raise ValueError("Multiple per-screen media files require cycle mode.")
        if len(assignment.media_files) > 1:
            for media_file in assignment.media_files:
                if Path(media_file).suffix.lower() not in supported_image_extensions:
                    raise ValueError("Image cycling supports image files only.")
        for media_file in assignment.media_files:
            if Path(media_file).suffix.lower() not in supported_media_extensions:
                raise ValueError(
                    "Only supported video and image files can be assigned."
                )
            if (
                video_directory is not None
                and not (video_directory / media_file).exists()
            ):
                raise ValueError(f"Missing assigned media file: {media_file}")


def validate_schedule_candidate(
    candidate: ScheduleEntry,
    schedules: list[ScheduleEntry],
    ignore_id: str | None = None,
    video_directory: Path | None = None,
    *,
    supported_media_extensions: set[str] = SUPPORTED_MEDIA_EXTENSIONS,
    supported_image_extensions: set[str] = SUPPORTED_IMAGE_EXTENSIONS,
) -> None:
    if not candidate.title.strip():
        raise ValueError("Add a title for the schedule.")
    if candidate.start_time == candidate.end_time:
        raise ValueError("Start time and end time cannot be the same.")
    if not candidate.days:
        raise ValueError("Select at least one day for this schedule.")
    if not candidate.video_file:
        raise ValueError("Choose a media file for this schedule.")

    if Path(candidate.video_file).suffix.lower() not in supported_media_extensions:
        raise ValueError("Only supported video and image files can be scheduled.")

    validate_schedule_media_assignments(
        candidate,
        video_directory,
        supported_media_extensions=supported_media_extensions,
        supported_image_extensions=supported_image_extensions,
    )

    for entry in schedules:
        if entry.id == ignore_id:
            continue
        if any(
            schedule_targets_overlap(candidate.screen_ids, entry.screen_ids)
            and candidate_day == entry_day
            and candidate_start < entry_end
            and entry_start < candidate_end
            for candidate_day, candidate_start, candidate_end in candidate.weekly_segments()
            for entry_day, entry_start, entry_end in entry.weekly_segments()
        ):
            raise ValueError(
                f'"{candidate.title}" overlaps with "{entry.title}" on at least one target screen.'
            )


def active_schedule_for_screen(
    schedules: list[ScheduleEntry],
    weekday_index: int,
    minute_of_day: int,
    screen_id: str,
    screen_groups: list[ScreenGroup],
) -> ScheduleEntry | None:
    for entry in sorted(
        schedules, key=lambda item: ScheduleEntry.time_to_minutes(item.start_time)
    ):
        if entry.covers_weekday_minute(
            weekday_index, minute_of_day
        ) and target_contains_screen(entry.screen_ids, screen_id, screen_groups):
            return entry
    return None


def referenced_video_files(schedules: list[ScheduleEntry]) -> set[str]:
    return {entry.video_file for entry in schedules if entry.video_file}
