from __future__ import annotations

from types import SimpleNamespace

import main as main_module
from models import ScheduleEntry, ScheduleMediaAssignment
from screen_protocols import ConfiguredScreen


def test_lan_remote_server_merge_remote_screens_moves_state_and_command() -> None:
    server = main_module.LanRemoteServer(port=9999)
    server._remote_screens = {
        "remote:source": {
            "screen_id": "remote:source",
            "name": "Source",
            "client_id": "client-a",
            "last_seen": 200.0,
            "last_command_version": 5,
            "online": True,
        },
        "remote:target": {
            "screen_id": "remote:target",
            "name": "Target",
            "client_id": "",
            "last_seen": 100.0,
            "last_command_version": 2,
            "online": True,
        },
    }
    server._commands = {
        "remote:source": {"screenId": "remote:source", "type": "play", "version": 7}
    }

    merged = server.merge_remote_screens("remote:source", "remote:target")

    assert merged is True
    assert "remote:source" not in server._remote_screens
    assert server._remote_screens["remote:target"]["client_id"] == "client-a"
    assert server._remote_screens["remote:target"]["last_command_version"] == 5
    assert server._commands["remote:target"].screen_id == "remote:target"


def test_controller_engine_handle_action_merge_duplicate_screen_remaps_references() -> (
    None
):
    entry = ScheduleEntry(
        id="sched-1",
        title="Morning",
        start_time="09:00",
        end_time="10:00",
        video_file="demo.jpg",
        video_label="demo.jpg",
        screen_ids=["remote:source"],
        days=["mon"],
        media_assignments={
            "remote:source": ScheduleMediaAssignment(
                screen_id="remote:source",
                media_files=["demo.jpg"],
                mode="single",
                interval_seconds=10,
            )
        },
    )

    class _RemoteServer:
        def merge_remote_screens(self, source: str, target: str) -> bool:
            return source == "remote:source" and target == "remote:target"

    class _Coordinator:
        def __init__(self) -> None:
            self.selected = []
            self.groups = []
            self.schedules = []
            self.virtual = set()

        def set_selected_monitors(self, ids):
            self.selected = list(ids)

        def set_screen_groups(self, groups):
            self.groups = list(groups)

        def set_schedules(self, schedules):
            self.schedules = list(schedules)

        def set_virtual_screen_ids(self, ids):
            self.virtual = set(ids)

    engine_like = SimpleNamespace(
        remote_server=_RemoteServer(),
        selected_monitor_ids=["remote:source"],
        enabled_screen_ids=["remote:source"],
        schedules=[entry],
        screen_groups=[],
        screen_aliases={"remote:source": "Lobby"},
        configured_screens=[
            ConfiguredScreen(
                id="remote:source",
                name="Source Screen",
                transport="browser",
                capabilities=["static_media"],
            )
        ],
        coordinator=_Coordinator(),
        all_screen_groups=lambda: [],
        browser_configured_screen_ids=lambda: {"remote:target"},
        persist_state=lambda: None,
        snapshot_for_channels=lambda _x: {"ok": True},
        notify_state_changed=lambda *args: 1,
    )

    response = main_module.ControllerEngine.handle_action(
        engine_like,
        "merge_duplicate_screen",
        {"source_screen_id": "remote:source", "target_screen_id": "remote:target"},
    )

    assert response["ok"] is True
    assert response["merged"] is True
    assert engine_like.selected_monitor_ids == ["remote:target"]
    assert engine_like.enabled_screen_ids == ["remote:target"]
    assert engine_like.schedules[0].screen_ids == ["remote:target"]
    assert "remote:target" in engine_like.schedules[0].media_assignments
    assert "remote:source" not in engine_like.schedules[0].media_assignments
    assert engine_like.screen_aliases["remote:target"] == "Lobby"
    assert "remote:source" not in engine_like.screen_aliases
