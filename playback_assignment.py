from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from models import DAY_INDEX_TO_KEY, ScheduleEntry
from playback_protocol import COMMAND_PROTOCOL_VERSION, PlaybackCommand
from scheduling import active_schedule_for_screen, schedule_assignment_for_screen
from screen_groups import ScreenGroup


def is_video_file(path: Path) -> bool:
    return path.suffix.lower() in {".mp4", ".mov", ".m4v", ".webm", ".ogg"}


def is_image_file(path: Path) -> bool:
    return path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


class PlaybackAssignmentService:
    def assignment_for_screen(
        self,
        *,
        screen_id: str,
        weekday_index: int,
        minute_of_day: int,
        quick_play_paths: dict[str, Path],
        quick_play_labels: dict[str, str],
        is_paused: bool,
        schedules: list[ScheduleEntry],
        screen_groups: list[ScreenGroup],
        video_directory: Path | None,
    ) -> dict[str, Any]:
        override_path = quick_play_paths.get(screen_id)
        if override_path is not None:
            return {
                "key": f"override:{screen_id}:{override_path.resolve()}:{is_paused}",
                "type": "play",
                "mode": "quick_play",
                "path": override_path,
                "label": quick_play_labels.get(screen_id, override_path.name),
                "entry_id": f"override:{screen_id}",
                "message": "",
            }

        entry = active_schedule_for_screen(
            schedules, weekday_index, minute_of_day, screen_id, screen_groups
        )
        if entry is None:
            return {
                "key": f"clear:{screen_id}",
                "type": "clear",
                "mode": "idle",
                "path": None,
                "label": "",
                "entry_id": None,
                "message": "No media scheduled for the current UK time.",
            }

        per_screen = schedule_assignment_for_screen(entry, screen_id)
        relative_paths = (
            per_screen.media_files[:] if per_screen is not None else [entry.video_file]
        )
        media_paths = [
            (video_directory / relative_path)
            for relative_path in relative_paths
            if video_directory is not None and relative_path
        ]
        if not media_paths or any(not path.exists() for path in media_paths):
            missing_label = (
                ", ".join(relative_paths)
                if relative_paths
                else (entry.video_label or entry.video_file)
            )
            return {
                "key": f"clear-missing:{screen_id}:{missing_label}",
                "type": "clear",
                "mode": "schedule",
                "path": None,
                "paths": [],
                "label": missing_label,
                "entry_id": entry.id,
                "message": f"Missing media file:\n{missing_label}",
            }

        cycle = (
            per_screen is not None
            and per_screen.mode == "cycle"
            and len(media_paths) > 1
        )
        assignment_key = "|".join(str(path.resolve()) for path in media_paths)
        return {
            "key": f"schedule:{entry.id}:{screen_id}:{assignment_key}:{is_paused}",
            "type": "play",
            "mode": "schedule",
            "path": media_paths[0],
            "paths": media_paths,
            "label": entry.video_label or relative_paths[0],
            "relative_path": relative_paths[0],
            "relative_paths": relative_paths,
            "entry_id": entry.id,
            "cycle": cycle,
            "cycle_interval_seconds": per_screen.interval_seconds
            if per_screen is not None
            else 10,
            "message": "",
        }

    def build_screen_command(
        self,
        *,
        screen_id: str,
        assignment: dict[str, Any],
        play_at_ms: int,
        version: int,
        transition_method: str,
        is_paused: bool,
    ) -> PlaybackCommand:
        media_path = assignment.get("path")
        media_kind = ""
        if isinstance(media_path, Path):
            if is_image_file(media_path):
                media_kind = "image"
            elif is_video_file(media_path):
                media_kind = "video"
        media_paths = [
            path for path in assignment.get("paths") or [] if isinstance(path, Path)
        ]
        relative_paths = [str(item) for item in assignment.get("relative_paths") or []]
        command_type = str(assignment.get("type", "clear")).strip().lower()
        mode = str(assignment.get("mode", "idle")).strip().lower()
        if command_type not in {"play", "clear"}:
            command_type = "clear"
        if mode not in {"schedule", "quick_play", "idle"}:
            mode = "idle"
        return PlaybackCommand(
            protocol_version=COMMAND_PROTOCOL_VERSION,
            screen_id=screen_id,
            version=max(0, int(version)),
            command_type=command_type,  # type: ignore[arg-type]
            mode=mode,  # type: ignore[arg-type]
            entry_id=(
                str(assignment.get("entry_id"))
                if assignment.get("entry_id") is not None
                else None
            ),
            message=str(assignment.get("message", "")),
            label=str(assignment.get("label", "")),
            path=str(media_path) if isinstance(media_path, Path) else "",
            paths=[str(path) for path in media_paths],
            relative_path=str(assignment.get("relative_path", "")),
            relative_paths=relative_paths,
            cycle=bool(assignment.get("cycle")),
            cycle_interval_seconds=int(assignment.get("cycle_interval_seconds") or 10),
            media_kind=media_kind,
            play_at_ms=int(play_at_ms),
            transition=str(transition_method or "fade_black"),
            paused=bool(is_paused),
        )

    def next_schedule_change_delay_ms(
        self,
        *,
        schedules: list[ScheduleEntry],
        now: datetime,
        tz,
    ) -> int | None:
        if not schedules:
            return None
        next_change: datetime | None = None
        for entry in schedules:
            start_minutes = ScheduleEntry.time_to_minutes(entry.start_time)
            end_minutes = ScheduleEntry.time_to_minutes(entry.end_time)
            for day_offset in range(8):
                day_date = (now + timedelta(days=day_offset)).date()
                day_key = DAY_INDEX_TO_KEY[day_date.weekday()]
                if day_key not in entry.selected_days():
                    continue
                start_dt = datetime.combine(
                    day_date, datetime.min.time(), tzinfo=tz
                ) + timedelta(minutes=start_minutes)
                if start_dt > now and (next_change is None or start_dt < next_change):
                    next_change = start_dt
                end_date = (
                    day_date
                    if start_minutes < end_minutes
                    else day_date + timedelta(days=1)
                )
                end_dt = datetime.combine(
                    end_date, datetime.min.time(), tzinfo=tz
                ) + timedelta(minutes=end_minutes)
                if end_dt > now and (next_change is None or end_dt < next_change):
                    next_change = end_dt
        if next_change is None:
            return None
        return max(int((next_change - now).total_seconds() * 1000) + 50, 250)
