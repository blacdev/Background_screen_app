from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import main as main_module
from models import ScheduleEntry


class _DummyListener:
    def __init__(self) -> None:
        self.seeded_version = 0
        self.seeded_channels: dict[str, int] = {}

    def seed_state_version(self, version: int) -> None:
        self.seeded_version = version

    def seed_channel_versions(self, versions: dict[str, int]) -> None:
        self.seeded_channels = dict(versions)


class _DummyToggle:
    def blockSignals(self, _value: bool) -> None:
        return None

    def setChecked(self, _value: bool) -> None:
        return None


class _DummyTransitionSelector:
    def __init__(self) -> None:
        self._index = 0

    def findData(self, _value: str) -> int:
        return 0

    def currentIndex(self) -> int:
        return self._index

    def blockSignals(self, _value: bool) -> None:
        return None

    def setCurrentIndex(self, value: int) -> None:
        self._index = int(value)


def _window_like(tmp_path: Path) -> SimpleNamespace:
    existing_schedule = ScheduleEntry.from_dict(
        {
            "id": "existing",
            "title": "Existing",
            "start_time": "09:00",
            "end_time": "10:00",
            "video_file": "a.jpg",
            "video_label": "a.jpg",
            "screen_ids": ["screen-a"],
            "days": ["mon"],
        }
    )
    return SimpleNamespace(
        backend_online=False,
        backend_state_version=0,
        backend_channel_versions={"config": 0, "library": 0, "runtime": 0},
        backend_listener=_DummyListener(),
        configured_screens=[],
        screen_groups=[],
        schedules=[existing_schedule],
        selected_monitor_ids=["screen-a"],
        enabled_screen_ids=[],
        video_directory=tmp_path,
        screen_aliases={},
        screen_registry=SimpleNamespace(configured_screen_name_map=lambda: {}),
        transition_method="fade_black",
        run_at_startup=False,
        available_videos=[],
        remote_screens={},
        backend_network_snapshot={},
        backend_library_scanning=False,
        backend_library_error="",
        backend_performance_metrics={},
        backend_playback_enabled=False,
        backend_paused=False,
        backend_window_count=0,
        run_at_startup_action=_DummyToggle(),
        transition_selector=_DummyTransitionSelector(),
        current_edit_id=None,
        refresh_monitors=lambda: None,
        load_schedule_into_form=lambda _id: None,
        update_status_labels=lambda _clock, _active: None,
        update_playback_state_label=lambda _paused, _count: None,
        update_network_summary=lambda: None,
        update_engine_diagnostics_summary=lambda: None,
    )


def test_apply_backend_snapshot_full_payload_keeps_legacy_behavior(
    tmp_path: Path,
) -> None:
    (tmp_path / "img.jpg").write_bytes(b"x")
    window = _window_like(tmp_path)

    snapshot = {
        "stateVersion": 5,
        "channelVersions": {"config": 2, "library": 3, "runtime": 4},
        "channels": ["config", "library", "runtime"],
        "configuredScreens": [],
        "screenGroups": [],
        "selectedMonitorIds": ["screen-a"],
        "enabledScreenIds": ["screen-a"],
        "videoDirectory": str(tmp_path),
        "screenAliases": {"screen-a": "Lobby"},
        "transitionMethod": "fade_black",
        "runAtStartup": True,
        "schedules": [
            {
                "id": "sched-1",
                "title": "Morning",
                "start_time": "09:00",
                "end_time": "10:00",
                "video_file": "img.jpg",
                "video_label": "img.jpg",
                "screen_ids": ["screen-a"],
                "days": ["mon"],
            }
        ],
        "availableMedia": [
            {
                "relativePath": "img.jpg",
                "name": "img.jpg",
                "category": "Root",
                "isVideo": False,
                "isImage": True,
            }
        ],
        "library": {"scanning": False, "error": "", "indexedCount": 1},
        "remoteScreens": [],
        "network": {},
        "status": {"clockLabel": "09:15:00", "activeLabel": "Morning"},
        "playback": {"enabled": True, "paused": False, "localWindowCount": 1},
        "performance": {"indexedFileCount": 1},
    }

    main_module.MainWindow.apply_backend_snapshot(window, snapshot)

    assert window.backend_state_version == 5
    assert window.backend_channel_versions == {"config": 2, "library": 3, "runtime": 4}
    assert len(window.schedules) == 1
    assert window.schedules[0].id == "sched-1"
    assert window.run_at_startup is True
    assert window.backend_playback_enabled is True


def test_apply_backend_snapshot_runtime_delta_does_not_overwrite_config(
    tmp_path: Path,
) -> None:
    window = _window_like(tmp_path)
    prior_schedule_ids = [entry.id for entry in window.schedules]
    prior_selected = list(window.selected_monitor_ids)

    runtime_only = {
        "stateVersion": 9,
        "channelVersions": {"config": 3, "library": 5, "runtime": 8},
        "channels": ["runtime"],
        "remoteScreens": [{"screen_id": "remote:abc", "online": True}],
        "network": {"preferred_url": "http://localhost:8765/tv"},
        "status": {"clockLabel": "10:00:00", "activeLabel": "No active schedule"},
        "playback": {"enabled": False, "paused": True, "localWindowCount": 0},
        "performance": {"indexedFileCount": 0},
    }

    main_module.MainWindow.apply_backend_snapshot(window, runtime_only)

    assert [entry.id for entry in window.schedules] == prior_schedule_ids
    assert window.selected_monitor_ids == prior_selected
    assert window.backend_paused is True
    assert "remote:abc" in window.remote_screens


def test_playback_coordinator_transition_change_forces_sync_for_command_refresh() -> (
    None
):
    calls: list[bool] = []
    coordinator = SimpleNamespace(
        transition_method="fade_black",
        manage_local_windows=False,
        last_assignment_keys={"screen-a": "same-key"},
        sync_current_entry=lambda force=False: calls.append(bool(force)),
    )

    main_module.PlaybackCoordinator.set_transition_method(coordinator, "cut")

    assert coordinator.transition_method == "cut"
    assert coordinator.last_assignment_keys == {}
    assert calls == [True]


def test_apply_backend_snapshot_runtime_noop_skips_broad_ui_refresh(
    tmp_path: Path,
) -> None:
    calls = {
        "refresh_monitors": 0,
        "update_network_summary": 0,
        "update_engine_diagnostics_summary": 0,
        "update_playback_state_label": 0,
        "update_status_labels": 0,
    }

    window = _window_like(tmp_path)
    window.remote_screens = {"remote:abc": {"screen_id": "remote:abc", "online": True}}
    window.backend_network_snapshot = {"preferred_url": "http://localhost:8765/tv"}
    window.backend_paused = False
    window.backend_playback_enabled = True
    window.backend_window_count = 1
    window.backend_performance_metrics = {"indexedFileCount": 1}
    window.refresh_monitors = lambda: calls.__setitem__(
        "refresh_monitors", calls["refresh_monitors"] + 1
    )
    window.update_network_summary = lambda: calls.__setitem__(
        "update_network_summary", calls["update_network_summary"] + 1
    )
    window.update_engine_diagnostics_summary = lambda: calls.__setitem__(
        "update_engine_diagnostics_summary",
        calls["update_engine_diagnostics_summary"] + 1,
    )
    window.update_playback_state_label = lambda _paused, _count: calls.__setitem__(
        "update_playback_state_label", calls["update_playback_state_label"] + 1
    )
    window.update_status_labels = lambda _clock, _active: calls.__setitem__(
        "update_status_labels", calls["update_status_labels"] + 1
    )

    runtime_noop = {
        "stateVersion": 10,
        "channelVersions": {"config": 3, "library": 5, "runtime": 9},
        "channels": ["runtime"],
        "remoteScreens": [{"screen_id": "remote:abc", "online": True}],
        "network": {"preferred_url": "http://localhost:8765/tv"},
        "status": {"clockLabel": "10:00:00", "activeLabel": "No active schedule"},
        "playback": {"enabled": True, "paused": False, "localWindowCount": 1},
        "performance": {"indexedFileCount": 1},
    }

    main_module.MainWindow.apply_backend_snapshot(window, runtime_noop)

    assert calls == {
        "refresh_monitors": 0,
        "update_network_summary": 0,
        "update_engine_diagnostics_summary": 0,
        "update_playback_state_label": 1,
        "update_status_labels": 1,
    }
