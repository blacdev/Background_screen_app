from __future__ import annotations

import main as main_module


def test_playback_window_uses_tool_mode_on_windows() -> None:
    assert main_module.playback_window_uses_tool_mode("win32") is True


def test_playback_window_avoids_tool_mode_off_windows() -> None:
    assert main_module.playback_window_uses_tool_mode("linux") is False


def test_playback_window_flags_include_required_surface_flags() -> None:
    flags = main_module.playback_window_flags_for_platform("win32")

    assert bool(flags & main_module.Qt.WindowType.Window)
    assert bool(flags & main_module.Qt.WindowType.FramelessWindowHint)
    assert bool(flags & main_module.Qt.WindowType.WindowStaysOnTopHint)
