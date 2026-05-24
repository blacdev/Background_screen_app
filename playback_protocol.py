from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

COMMAND_PROTOCOL_VERSION = 2

CommandType = Literal["play", "clear"]
CommandMode = Literal["schedule", "quick_play", "idle"]


@dataclass(slots=True)
class PlaybackCommand:
    protocol_version: int
    screen_id: str
    version: int
    command_type: CommandType
    mode: CommandMode
    entry_id: str | None = None
    message: str = ""
    label: str = ""
    path: str = ""
    paths: list[str] = field(default_factory=list)
    relative_path: str = ""
    relative_paths: list[str] = field(default_factory=list)
    cycle: bool = False
    cycle_interval_seconds: int = 10
    media_kind: str = ""
    media_url: str = ""
    media_urls: list[str] = field(default_factory=list)
    play_at_ms: int = 0
    transition: str = "fade_black"
    paused: bool = False

    @classmethod
    def clear(
        cls, screen_id: str, *, version: int = 0, message: str = ""
    ) -> "PlaybackCommand":
        return cls(
            protocol_version=COMMAND_PROTOCOL_VERSION,
            screen_id=screen_id,
            version=max(0, int(version)),
            command_type="clear",
            mode="idle",
            message=message,
        )

    @classmethod
    def from_payload(
        cls, payload: object, *, default_screen_id: str = ""
    ) -> "PlaybackCommand":
        data = payload if isinstance(payload, dict) else {}
        protocol_version = int(
            data.get("protocolVersion") or data.get("protocol_version") or 1
        )
        screen_id = str(
            data.get("screenId") or data.get("screen_id") or default_screen_id
        )
        command_type = (
            str(data.get("type") or data.get("command_type") or "clear").strip().lower()
        )
        mode = str(data.get("mode") or "idle").strip().lower()
        if command_type not in {"play", "clear"}:
            command_type = "clear"
        if mode not in {"schedule", "quick_play", "idle"}:
            mode = "idle"
        paths = [str(item) for item in (data.get("paths") or []) if str(item)]
        relative_paths = [
            str(item)
            for item in (data.get("relativePaths") or data.get("relative_paths") or [])
            if str(item)
        ]
        return cls(
            protocol_version=max(0, protocol_version),
            screen_id=screen_id,
            version=max(0, int(data.get("version") or 0)),
            command_type=command_type,  # type: ignore[arg-type]
            mode=mode,  # type: ignore[arg-type]
            entry_id=(
                str(data.get("entryId"))
                if data.get("entryId") is not None
                else (
                    str(data.get("entry_id"))
                    if data.get("entry_id") is not None
                    else None
                )
            ),
            message=str(data.get("message") or ""),
            label=str(data.get("label") or ""),
            path=str(data.get("path") or ""),
            paths=paths,
            relative_path=str(
                data.get("relativePath") or data.get("relative_path") or ""
            ),
            relative_paths=relative_paths,
            cycle=bool(data.get("cycle")),
            cycle_interval_seconds=max(
                2,
                int(
                    data.get("cycleIntervalSeconds")
                    or data.get("cycle_interval_seconds")
                    or 10
                ),
            ),
            media_kind=str(data.get("mediaKind") or data.get("media_kind") or ""),
            media_url=str(data.get("mediaUrl") or data.get("media_url") or ""),
            media_urls=[
                str(item)
                for item in (data.get("mediaUrls") or data.get("media_urls") or [])
                if str(item)
            ],
            play_at_ms=max(0, int(data.get("playAtMs") or data.get("play_at_ms") or 0)),
            transition=str(data.get("transition") or "fade_black"),
            paused=bool(data.get("paused")),
        )

    def to_engine_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "screen_id": self.screen_id,
            "version": self.version,
            "type": self.command_type,
            "mode": self.mode,
            "entry_id": self.entry_id,
            "message": self.message,
            "label": self.label,
            "path": self.path,
            "paths": self.paths[:],
            "relative_path": self.relative_path,
            "relative_paths": self.relative_paths[:],
            "cycle": self.cycle,
            "cycle_interval_seconds": self.cycle_interval_seconds,
            "media_kind": self.media_kind,
            "media_url": self.media_url,
            "media_urls": self.media_urls[:],
            "play_at_ms": self.play_at_ms,
            "transition": self.transition,
            "paused": self.paused,
        }

    def to_remote_dict(
        self, *, media_url: str = "", media_urls: list[str] | None = None
    ) -> dict[str, Any]:
        return {
            "protocolVersion": self.protocol_version,
            "screenId": self.screen_id,
            "version": self.version,
            "type": self.command_type,
            "mode": self.mode,
            "entryId": self.entry_id,
            "message": self.message,
            "label": self.label,
            "path": self.path,
            "paths": self.paths[:],
            "relativePath": self.relative_path,
            "relativePaths": self.relative_paths[:],
            "mediaKind": self.media_kind,
            "mediaUrl": media_url or self.media_url,
            "mediaUrls": list(media_urls or self.media_urls),
            "cycle": self.cycle,
            "cycleIntervalSeconds": self.cycle_interval_seconds,
            "playAtMs": self.play_at_ms,
            "transition": self.transition,
            "paused": self.paused,
        }


def command_paths_as_path_objects(
    command: PlaybackCommand,
) -> tuple[Path | None, list[Path]]:
    media_path = Path(command.path).expanduser() if command.path else None
    media_paths = [Path(item).expanduser() for item in command.paths if item]
    return media_path, media_paths
