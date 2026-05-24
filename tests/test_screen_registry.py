from __future__ import annotations

from types import SimpleNamespace

import screen_registry as screen_registry_module
from screen_groups import ScreenGroup
from screen_protocols import ConfiguredScreen
from screen_registry import ScreenRegistry


class _DummyGeometry:
    def __init__(self, width: int, height: int) -> None:
        self._width = width
        self._height = height

    def width(self) -> int:
        return self._width

    def height(self) -> int:
        return self._height


class _DummyScreen:
    def __init__(self, identifier: str, width: int = 1920, height: int = 1080) -> None:
        self.identifier = identifier
        self._geometry = _DummyGeometry(width, height)

    def geometry(self) -> _DummyGeometry:
        return self._geometry


def test_screen_registry_facade_reads_live_owner_state() -> None:
    owner = SimpleNamespace(
        configured_screens=[
            ConfiguredScreen(
                id="browser-1",
                name="Lobby",
                transport="browser",
                capabilities=["static_media", "playlist"],
            ),
            ConfiguredScreen(
                id="local-1",
                name="Control",
                transport="local_hdmi",
                capabilities=["live_view", "sync_playback"],
            ),
        ],
        screen_groups=[
            ScreenGroup(id="group:lobby", name="Lobby Group", screen_ids=["browser-1"])
        ],
        selected_monitor_ids=["monitor-a"],
        enabled_screen_ids=["browser-1"],
        screen_aliases={"browser-1": "Front Lobby"},
    )

    registry = ScreenRegistry(owner)

    assert registry.configured_screen_name_map() == {
        "browser-1": "Lobby",
        "local-1": "Control",
    }
    assert registry.browser_configured_screen_ids() == {"browser-1"}
    assert registry.static_media_configured_screen_ids() == {"browser-1"}
    assert [group.id for group in registry.all_screen_groups()] == [
        "group:default",
        "group:lobby",
    ]

    owner.configured_screens = [
        ConfiguredScreen(
            id="browser-2",
            name="Reception",
            transport="browser",
            capabilities=["static_media"],
        )
    ]
    owner.selected_monitor_ids = ["monitor-b"]
    owner.screen_groups = []

    assert registry.browser_configured_screen_ids() == {"browser-2"}
    assert registry.static_media_configured_screen_ids() == {"browser-2"}
    assert [group.screen_ids for group in registry.all_screen_groups()] == [
        ["monitor-b"]
    ]


def test_screen_registry_unified_targets_include_local_and_online_remote(
    monkeypatch,
) -> None:
    owner = SimpleNamespace(
        configured_screens=[
            ConfiguredScreen(
                id="browser-1",
                name="Lobby Screen",
                transport="browser",
                capabilities=["static_media"],
            )
        ],
        screen_groups=[],
        selected_monitor_ids=[],
        enabled_screen_ids=[],
        screen_aliases={"local-1": "Front TV"},
        remote_screens={"browser-1": {"online": True}},
    )

    monkeypatch.setattr(
        screen_registry_module,
        "browser_target_status_detail",
        lambda _screen, _remote: (True, "Browser Receiver • Online", ""),
    )

    registry = ScreenRegistry(owner)
    local_screens = [_DummyScreen("local-1", 1366, 768)]
    targets = registry.unified_screen_targets(
        local_screens=local_screens,
        screen_identifier=lambda screen: screen.identifier,
        screen_display_name=lambda screen, aliases: str(
            aliases.get(screen.identifier, screen.identifier)
        ),
        include_offline=True,
    )

    assert [target.id for target in targets] == ["local-1", "browser-1"]
    assert targets[0].label == "Front TV"
    assert targets[0].kind == "local"
    assert "1366x768" in targets[0].detail
    assert targets[1].kind == "remote"
    assert targets[1].online is True


def test_screen_registry_unified_targets_exclude_offline_remote_when_requested(
    monkeypatch,
) -> None:
    owner = SimpleNamespace(
        configured_screens=[
            ConfiguredScreen(
                id="browser-1",
                name="Lobby Screen",
                transport="browser",
                capabilities=["static_media"],
            )
        ],
        screen_groups=[],
        selected_monitor_ids=[],
        enabled_screen_ids=[],
        screen_aliases={},
        remote_screens={"browser-1": {"online": False}},
    )

    monkeypatch.setattr(
        screen_registry_module,
        "browser_target_status_detail",
        lambda _screen, _remote: (False, "Browser Receiver • Offline", "Offline"),
    )

    registry = ScreenRegistry(owner)
    targets = registry.unified_screen_targets(
        local_screens=[],
        screen_identifier=lambda screen: screen.identifier,
        screen_display_name=lambda screen, aliases: str(
            aliases.get(screen.identifier, screen.identifier)
        ),
        include_offline=False,
    )

    assert targets == []
