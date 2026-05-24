from __future__ import annotations

from playback_protocol import COMMAND_PROTOCOL_VERSION, PlaybackCommand


def test_playback_command_round_trip_engine_and_remote_dicts() -> None:
    command = PlaybackCommand(
        protocol_version=COMMAND_PROTOCOL_VERSION,
        screen_id="screen-a",
        version=3,
        command_type="play",
        mode="schedule",
        entry_id="sched-1",
        message="",
        label="Demo",
        path="C:/media/demo.jpg",
        paths=["C:/media/demo.jpg"],
        relative_path="demo.jpg",
        relative_paths=["demo.jpg"],
        cycle=False,
        cycle_interval_seconds=10,
        media_kind="image",
        play_at_ms=123,
        transition="fade_black",
        paused=False,
    )

    engine_payload = command.to_engine_dict()
    parsed = PlaybackCommand.from_payload(
        {
            "protocolVersion": COMMAND_PROTOCOL_VERSION,
            "screenId": "screen-a",
            "version": 3,
            "type": "play",
            "mode": "schedule",
            "entryId": "sched-1",
            "label": "Demo",
            "path": "C:/media/demo.jpg",
            "paths": ["C:/media/demo.jpg"],
            "relativePath": "demo.jpg",
            "relativePaths": ["demo.jpg"],
            "cycle": False,
            "cycleIntervalSeconds": 10,
            "mediaKind": "image",
            "playAtMs": 123,
            "transition": "fade_black",
            "paused": False,
        }
    )

    assert engine_payload["protocol_version"] == COMMAND_PROTOCOL_VERSION
    assert parsed.screen_id == "screen-a"
    assert parsed.command_type == "play"


def test_playback_command_from_payload_accepts_legacy_v1_without_protocol_version() -> (
    None
):
    parsed = PlaybackCommand.from_payload(
        {
            "screenId": "screen-a",
            "version": 2,
            "type": "clear",
            "mode": "idle",
            "message": "No media",
        }
    )
    assert parsed.protocol_version == 1
    assert parsed.command_type == "clear"
    assert parsed.mode == "idle"
