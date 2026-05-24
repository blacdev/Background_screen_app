from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import main as main_module


class _DummyTimer:
    def __init__(self, active: bool = False) -> None:
        self.active = active
        self.start_calls = 0
        self.stop_calls = 0

    def isActive(self) -> bool:
        return self.active

    def start(self) -> None:
        self.active = True
        self.start_calls += 1

    def stop(self) -> None:
        self.active = False
        self.stop_calls += 1


def _make_window(
    *, current_media_kind: str, is_paused: bool, current_video_path: object
) -> Any:
    window = SimpleNamespace(
        current_media_kind=current_media_kind,
        is_paused=is_paused,
        current_video_path=current_video_path,
        playback_watchdog_timer=_DummyTimer(),
    )
    window.should_run_playback_watchdog = lambda: (
        main_module.PlaybackWindow.should_run_playback_watchdog(cast(Any, window))
    )
    return window


def test_should_run_playback_watchdog_only_for_active_unpaused_video() -> None:
    playing_video = _make_window(
        current_media_kind="video", is_paused=False, current_video_path=object()
    )
    paused_video = _make_window(
        current_media_kind="video", is_paused=True, current_video_path=object()
    )
    image_window = _make_window(
        current_media_kind="image", is_paused=False, current_video_path=None
    )
    idle_window = _make_window(
        current_media_kind="", is_paused=False, current_video_path=None
    )

    assert (
        main_module.PlaybackWindow.should_run_playback_watchdog(playing_video) is True
    )
    assert (
        main_module.PlaybackWindow.should_run_playback_watchdog(paused_video) is False
    )
    assert (
        main_module.PlaybackWindow.should_run_playback_watchdog(image_window) is False
    )
    assert main_module.PlaybackWindow.should_run_playback_watchdog(idle_window) is False


def test_sync_playback_watchdog_timer_starts_for_active_video() -> None:
    window = _make_window(
        current_media_kind="video", is_paused=False, current_video_path=object()
    )

    main_module.PlaybackWindow.sync_playback_watchdog_timer(window)

    assert window.playback_watchdog_timer.active is True
    assert window.playback_watchdog_timer.start_calls == 1
    assert window.playback_watchdog_timer.stop_calls == 0


def test_sync_playback_watchdog_timer_stops_for_idle_or_image_state() -> None:
    window = _make_window(
        current_media_kind="image", is_paused=False, current_video_path=None
    )
    window.playback_watchdog_timer.active = True

    main_module.PlaybackWindow.sync_playback_watchdog_timer(window)

    assert window.playback_watchdog_timer.active is False
    assert window.playback_watchdog_timer.start_calls == 0
    assert window.playback_watchdog_timer.stop_calls == 1
