from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import main as main_module


class _DummyTrayIcon:
    def __init__(self, visible: bool = True) -> None:
        self._visible = visible
        self.messages: list[tuple[str, str, object, int]] = []
        self.hidden = False
        self.deleted = False

    def isVisible(self) -> bool:
        return self._visible

    def showMessage(
        self, title: str, message: str, icon: object, timeout_ms: int
    ) -> None:
        self.messages.append((title, message, icon, timeout_ms))

    def hide(self) -> None:
        self.hidden = True

    def deleteLater(self) -> None:
        self.deleted = True


class _DummyTimer:
    def __init__(self) -> None:
        self.stopped = False
        self.started = False

    def stop(self) -> None:
        self.stopped = True

    def start(self) -> None:
        self.started = True


class _DummyWatcher:
    def __init__(self) -> None:
        self._directories = ["watched-a", "watched-b"]
        self.removed: list[list[str]] = []

    def directories(self) -> list[str]:
        return list(self._directories)

    def removePaths(self, paths: list[str]) -> None:
        self.removed.append(list(paths))
        self._directories = [path for path in self._directories if path not in paths]


class _DummyBackendClient:
    def __init__(self, online: bool = True) -> None:
        self.online = online
        self.actions: list[tuple[str, dict[str, object], float | None]] = []

    def ping(self) -> bool:
        return self.online

    def action(
        self, name: str, payload: dict[str, object], timeout: float | None = None
    ):
        self.actions.append((name, dict(payload), timeout))
        return {"ok": True}


class _DummyTransitionSelector:
    def __init__(self, value: str) -> None:
        self.value = value

    def currentData(self) -> str:
        return self.value


class _DummyEvent:
    def __init__(self) -> None:
        self.accepted = False

    def accept(self) -> None:
        self.accepted = True


class _DummyApp:
    def __init__(self) -> None:
        self.exit_calls: list[int] = []

    def exit(self, code: int) -> None:
        self.exit_calls.append(code)


def _make_window() -> Any:
    window = SimpleNamespace()
    window._allow_real_quit = False
    window._shutdown_in_progress = False
    window._low_value_timers_paused = False
    window.backend_listener = SimpleNamespace(stop=lambda: None)
    window.ui_status_timer = _DummyTimer()
    window.folder_refresh_timer = _DummyTimer()
    window.folder_watcher = _DummyWatcher()
    window.tray_icon = None
    window.backend_client = _DummyBackendClient()
    window.prepare_calls = []
    window.close_calls = 0
    window.fullscreen_action = SimpleNamespace(isChecked=lambda: False)
    window.showMinimized = lambda: setattr(window, "minimized", True)
    window.hide = lambda: setattr(window, "hidden", True)
    window.showNormal = lambda: setattr(window, "shown_normal", True)
    window.showFullScreen = lambda: setattr(window, "shown_fullscreen", True)
    window.raise_ = lambda: setattr(window, "raised", True)
    window.activateWindow = lambda: setattr(window, "activated", True)
    window.close = lambda: setattr(window, "close_calls", window.close_calls + 1)
    window.prepare_for_exit = lambda stop_engine: window.prepare_calls.append(
        stop_engine
    )
    window._set_low_value_timers_paused = lambda paused: (
        main_module.MainWindow._set_low_value_timers_paused(window, paused)
    )
    window._pending_transition_method = None
    window.backend_online = False
    window.transition_selector = _DummyTransitionSelector("fade_black")
    window.transition_method = "fade_black"
    window.set_form_message = lambda _text, _kind="": None
    window.call_backend_action = lambda _action, _payload=None, **_kwargs: {"ok": True}
    return window


def test_send_to_background_does_nothing_when_real_quit_is_allowed() -> None:
    window = _make_window()
    window._allow_real_quit = True

    main_module.MainWindow.send_to_background(window)

    assert getattr(window, "minimized", False) is False
    assert getattr(window, "hidden", False) is False


def test_send_to_background_minimizes_when_controller_has_no_visible_tray_icon() -> (
    None
):
    window = _make_window()

    main_module.MainWindow.send_to_background(window)

    assert getattr(window, "minimized", False) is True
    assert getattr(window, "hidden", False) is False


def test_send_to_background_hides_window_and_shows_status_when_tray_is_visible() -> (
    None
):
    window = _make_window()
    window.tray_icon = _DummyTrayIcon(visible=True)

    main_module.MainWindow.send_to_background(window)

    assert getattr(window, "hidden", False) is True
    assert window.tray_icon.messages == [
        (
            "Background Screen Controller",
            "The controller is still running in the background.",
            main_module.QSystemTrayIcon.MessageIcon.Information,
            2000,
        )
    ]


def test_show_from_tray_restores_and_activates_controller() -> None:
    window = _make_window()

    main_module.MainWindow.show_from_tray(window)

    assert getattr(window, "shown_normal", False) is True
    assert getattr(window, "raised", False) is True
    assert getattr(window, "activated", False) is True
    assert getattr(window, "shown_fullscreen", False) is False


def test_low_value_timers_pause_and_resume() -> None:
    window = _make_window()

    main_module.MainWindow._set_low_value_timers_paused(window, True)

    assert window._low_value_timers_paused is True
    assert window.ui_status_timer.stopped is True
    assert window.folder_refresh_timer.stopped is True

    main_module.MainWindow._set_low_value_timers_paused(window, False)

    assert window._low_value_timers_paused is False
    assert window.ui_status_timer.started is True


def test_transition_change_is_buffered_when_backend_offline() -> None:
    window = _make_window()
    window.backend_online = False
    window.transition_selector = _DummyTransitionSelector("soft_fade")
    messages: list[tuple[str, str]] = []
    window.set_form_message = lambda text, kind="": messages.append((text, kind))

    main_module.MainWindow.on_transition_changed(window)

    assert window._pending_transition_method == "soft_fade"
    assert window.transition_method == "soft_fade"
    assert messages


def test_flush_pending_transition_applies_when_backend_online() -> None:
    window = _make_window()
    window.backend_online = True
    window._pending_transition_method = "cut"
    calls: list[tuple[str, dict[str, object], bool]] = []

    def _call_backend_action(
        action: str, payload: dict[str, object], *, show_errors: bool = True
    ):
        calls.append((action, dict(payload), show_errors))
        return {"ok": True}

    window.call_backend_action = _call_backend_action

    main_module.MainWindow._flush_pending_transition_method(window)

    assert calls == [("set_transition_method", {"transition_method": "cut"}, False)]
    assert window._pending_transition_method is None


def test_close_event_exits_controller_without_requesting_engine_shutdown(
    monkeypatch,
) -> None:
    window = _make_window()
    event = _DummyEvent()
    dummy_app = _DummyApp()
    monkeypatch.setattr(main_module.QApplication, "instance", lambda: dummy_app)

    main_module.MainWindow.closeEvent(window, event)

    assert window.prepare_calls == [False]
    assert window._allow_real_quit is True
    assert event.accepted is True
    assert dummy_app.exit_calls == [0]


def test_quit_from_tray_requests_engine_shutdown_before_exiting(monkeypatch) -> None:
    window = _make_window()
    dummy_app = _DummyApp()
    monkeypatch.setattr(main_module.QApplication, "instance", lambda: dummy_app)

    main_module.MainWindow.quit_from_tray(window)

    assert window.prepare_calls == [True]
    assert window._allow_real_quit is True
    assert window._shutdown_in_progress is True
    assert window.close_calls == 1
    assert dummy_app.exit_calls == [0]
