from __future__ import annotations

import pytest

from browser_screen_bindings import (
    BROWSER_BINDING_KEY,
    LEGACY_BINDING_KEY,
    bound_remote_state,
    browser_binding_remote_id,
    browser_target_status_detail,
    configured_browser_screen_ids,
    resolve_browser_commands,
    validate_unique_browser_bindings,
)
from screen_protocols import ConfiguredScreen, supported_capabilities


def make_browser_screen(screen_id: str, name: str, binding: dict[str, str] | None = None) -> ConfiguredScreen:
    return ConfiguredScreen(
        id=screen_id,
        name=name,
        transport="browser",
        capabilities=list(supported_capabilities("browser")),
        binding=binding or {},
    )


def test_browser_binding_remote_id_prefers_explicit_browser_binding_key() -> None:
    screen = make_browser_screen(
        "screen-1",
        "TV",
        {BROWSER_BINDING_KEY: "remote:abc", LEGACY_BINDING_KEY: "remote:legacy"},
    )
    assert browser_binding_remote_id(screen) == "remote:abc"


def test_browser_binding_remote_id_supports_legacy_binding_value() -> None:
    screen = make_browser_screen("screen-1", "TV", {LEGACY_BINDING_KEY: "remote:legacy"})
    assert browser_binding_remote_id(screen) == "remote:legacy"


def test_browser_binding_remote_id_ignores_non_browser_screens() -> None:
    screen = ConfiguredScreen(
        id="screen-2",
        name="Projector",
        transport="dlna",
        capabilities=list(supported_capabilities("dlna")),
        binding={BROWSER_BINDING_KEY: "remote:abc"},
    )
    assert browser_binding_remote_id(screen) == ""


def test_configured_browser_screen_ids_returns_only_browser_ids() -> None:
    screens = [
        make_browser_screen("screen-1", "TV 1"),
        ConfiguredScreen(
            id="screen-2",
            name="Projector",
            transport="miracast",
            capabilities=list(supported_capabilities("miracast")),
        ),
        make_browser_screen("screen-3", "TV 2"),
    ]
    assert configured_browser_screen_ids(screens) == {"screen-1", "screen-3"}


def test_validate_unique_browser_bindings_allows_unbound_entries() -> None:
    validate_unique_browser_bindings([make_browser_screen("screen-1", "TV 1"), make_browser_screen("screen-2", "TV 2")])


def test_validate_unique_browser_bindings_rejects_duplicate_remote_bindings() -> None:
    with pytest.raises(ValueError, match="only be bound to one"):
        validate_unique_browser_bindings(
            [
                make_browser_screen("screen-1", "TV 1", {BROWSER_BINDING_KEY: "remote:abc"}),
                make_browser_screen("screen-2", "TV 2", {LEGACY_BINDING_KEY: "remote:abc"}),
            ]
        )


def test_bound_remote_state_returns_copy_of_matching_remote_state() -> None:
    remote_screens = {"remote:abc": {"screen_id": "remote:abc", "online": True}}
    state = bound_remote_state(make_browser_screen("screen-1", "TV", {BROWSER_BINDING_KEY: "remote:abc"}), remote_screens)
    assert state == {"screen_id": "remote:abc", "online": True}
    assert state is not remote_screens["remote:abc"]


def test_bound_remote_state_returns_none_for_unbound_or_missing_state() -> None:
    assert bound_remote_state(make_browser_screen("screen-1", "TV"), {"remote:abc": {}}) is None
    assert bound_remote_state(make_browser_screen("screen-1", "TV", {BROWSER_BINDING_KEY: "remote:missing"}), {}) is None


def test_browser_target_status_detail_for_unbound_screen() -> None:
    online, detail, warning = browser_target_status_detail(make_browser_screen("screen-1", "TV"), {})
    assert online is False
    assert detail == "Browser receiver • Unbound"
    assert "Choose a connected browser receiver" in warning


def test_browser_target_status_detail_for_missing_bound_receiver() -> None:
    online, detail, warning = browser_target_status_detail(
        make_browser_screen("screen-1", "TV", {BROWSER_BINDING_KEY: "remote:abc"}),
        {},
    )
    assert online is False
    assert detail == "Browser receiver • Bound to remote:abc"
    assert "not currently connected" in warning


def test_browser_target_status_detail_for_online_receiver() -> None:
    online, detail, warning = browser_target_status_detail(
        make_browser_screen("screen-1", "TV", {BROWSER_BINDING_KEY: "remote:abc"}),
        {"remote:abc": {"online": True, "ip": "192.168.0.44", "width": 1920, "height": 1080}},
    )
    assert online is True
    assert detail == "Browser receiver • Online • 192.168.0.44 • 1920x1080"
    assert warning == ""


def test_browser_target_status_detail_for_offline_receiver() -> None:
    online, detail, warning = browser_target_status_detail(
        make_browser_screen("screen-1", "TV", {BROWSER_BINDING_KEY: "remote:abc"}),
        {"remote:abc": {"online": False, "ip": "192.168.0.44"}},
    )
    assert online is False
    assert detail == "Browser receiver • Offline • 192.168.0.44"
    assert warning == "The bound browser receiver is offline."


def test_browser_target_status_detail_without_ip_keeps_size_only() -> None:
    online, detail, warning = browser_target_status_detail(
        make_browser_screen("screen-1", "TV", {BROWSER_BINDING_KEY: "remote:abc"}),
        {"remote:abc": {"online": True, "width": 1280, "height": 720}},
    )
    assert online is True
    assert detail == "Browser receiver • Online • 1280x720"
    assert warning == ""


def test_resolve_browser_commands_maps_configured_browser_screen_to_bound_remote_id() -> None:
    configured_screens = [make_browser_screen("screen-1", "TV", {BROWSER_BINDING_KEY: "remote:abc"})]
    remote_screens = {"remote:abc": {"screen_id": "remote:abc", "online": True}}
    commands = {"screen-1": {"type": "play", "label": "Demo", "screenId": "screen-1"}}

    resolved = resolve_browser_commands(configured_screens, remote_screens, commands)

    assert resolved == {
        "remote:abc": {
            "type": "play",
            "label": "Demo",
            "screenId": "remote:abc",
            "configuredScreenId": "screen-1",
            "configuredScreenName": "TV",
        }
    }


def test_resolve_browser_commands_ignores_unbound_and_non_browser_commands() -> None:
    configured_screens = [
        make_browser_screen("screen-1", "TV"),
        ConfiguredScreen(
            id="screen-2",
            name="Projector",
            transport="dlna",
            capabilities=list(supported_capabilities("dlna")),
        ),
    ]
    remote_screens = {"remote:abc": {"screen_id": "remote:abc", "online": True}}
    commands = {
        "screen-1": {"type": "play"},
        "screen-2": {"type": "play"},
        "screen-3": {"type": "play"},
    }

    assert resolve_browser_commands(configured_screens, remote_screens, commands) == {}
