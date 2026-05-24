from __future__ import annotations

from types import SimpleNamespace

import main as main_module
from main import ControllerEngine


class _DummyScreen:
    def __init__(self, screen_id: str) -> None:
        self.id = screen_id
        self.label = screen_id
        self.kind = "local"
        self.online = True
        self.detail = ""
        self.warning = ""


def _engine_like() -> SimpleNamespace:
    return SimpleNamespace(
        _state_version=7,
        _channel_versions={"config": 3, "library": 5, "runtime": 11},
        configured_screens=[],
        screen_groups=[],
        selected_monitor_ids=[],
        enabled_screen_ids=[],
        video_directory=None,
        screen_aliases={},
        transition_method="fade_black",
        run_at_startup=False,
        lan_pairing_required=False,
        lan_allow_unpaired_clients=False,
        lan_paired_client_ids=[],
        schedules=[],
        media_scan_in_progress=False,
        media_scan_error="",
        available_videos=[],
        remote_screens={},
        coordinator=SimpleNamespace(playback_enabled=False),
        playback_paused=False,
        local_window_count=0,
        status_clock_label="12:00:00",
        status_active_label="No active schedule",
        last_snapshot_generated_at="",
        last_snapshot_size_bytes=0,
        channel_versions_snapshot=lambda: {"config": 3, "library": 5, "runtime": 11},
        available_media_payload=lambda: [],
        unified_screen_targets=lambda include_offline=True: [_DummyScreen("screen-a")],
        current_status_payload=lambda: {
            "clockLabel": "12:00:00",
            "activeLabel": "No active schedule",
        },
        performance_metrics_payload=lambda: {"indexedFileCount": 0},
        remote_server=SimpleNamespace(
            remote_screens_snapshot=lambda: [],
            network_snapshot=lambda: {},
        ),
    )


def test_snapshot_for_channels_full_when_no_client_versions() -> None:
    engine = _engine_like()
    snapshot = ControllerEngine.snapshot_for_channels(engine, None)

    assert snapshot["stateVersion"] == 7
    assert set(snapshot["channels"]) == {"config", "library", "runtime"}
    assert "configuredScreens" in snapshot
    assert "availableMedia" in snapshot
    assert "playback" in snapshot


def test_snapshot_for_channels_runtime_only_when_client_up_to_date_except_runtime() -> (
    None
):
    engine = _engine_like()
    snapshot = ControllerEngine.snapshot_for_channels(
        engine,
        {"config": 3, "library": 5, "runtime": 10},
    )

    assert snapshot["channels"] == ["runtime"]
    assert "playback" in snapshot
    assert "availableMedia" not in snapshot
    assert "configuredScreens" not in snapshot


def test_notify_state_changed_increments_selected_channels() -> None:
    class _DummyCondition:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def notify_all(self) -> None:
            return None

    engine = SimpleNamespace(
        _channel_versions={"config": 2, "library": 4, "runtime": 6},
        _state_condition=_DummyCondition(),
        _state_version=1,
    )

    version = ControllerEngine.notify_state_changed(engine, "library", "runtime")

    assert version == 2
    assert engine._channel_versions == {"config": 2, "library": 5, "runtime": 7}


def test_controller_api_wait_for_state_update_sends_channel_versions(
    monkeypatch,
) -> None:
    client = main_module.ControllerApiClient(port=1234)

    captured = {}

    def _fake_request(method: str, path: str, payload=None, timeout: float = 0.0):
        captured["method"] = method
        captured["path"] = path
        return {"ok": True}

    monkeypatch.setattr(client, "_request", _fake_request)
    client.wait_for_state_update(
        9, timeout=2.0, channel_versions={"config": 1, "library": 2, "runtime": 3}
    )

    assert captured["method"] == "GET"
    assert "since=9" in captured["path"]
    assert "c_config=1" in captured["path"]
    assert "c_library=2" in captured["path"]
    assert "c_runtime=3" in captured["path"]
