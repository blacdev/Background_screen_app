from __future__ import annotations

import pytest

from screen_protocols import (
    CAPABILITY_LABELS,
    ConfiguredScreen,
    SCREEN_CAPABILITIES,
    SCREEN_TRANSPORTS,
    TRANSPORT_LABELS,
    normalize_capability,
    normalize_transport,
    normalized_capabilities,
    parse_configured_screens,
    supported_capabilities,
    unsupported_capabilities,
    validate_configured_screens,
)


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("local", "local_hdmi"),
        ("Local HDMI", "local_hdmi"),
        ("browser_receiver", "browser"),
        ("web", "browser"),
        ("unknown", ""),
    ],
)
def test_normalize_transport(raw_value: str, expected: str) -> None:
    assert normalize_transport(raw_value) == expected


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("media", "static_media"),
        ("image video", "static_media"),
        ("sequential playback", "playlist"),
        ("live stream", "live_view"),
        ("input", "remote_input"),
        ("sync", "sync_playback"),
        ("unknown", ""),
    ],
)
def test_normalize_capability(raw_value: str, expected: str) -> None:
    assert normalize_capability(raw_value) == expected


def test_supported_capabilities_follow_declared_order() -> None:
    assert supported_capabilities("local_hdmi") == ("static_media", "playlist", "live_view", "sync_playback")
    assert supported_capabilities("invalid") == ()
    assert set(SCREEN_CAPABILITIES) >= set(supported_capabilities("browser"))
    assert set(SCREEN_TRANSPORTS) == {"local_hdmi", "browser"}


def test_normalized_capabilities_deduplicates_and_filters_unknown_values() -> None:
    assert normalized_capabilities(["media", "playlist", "media", "unknown", "sync"]) == [
        "static_media",
        "playlist",
        "sync_playback",
    ]


def test_unsupported_capabilities_reports_only_invalid_items_for_transport() -> None:
    assert unsupported_capabilities("browser", ["static_media", "live_view", "sync_playback"]) == []


def test_configured_screen_round_trips_and_defaults_capabilities() -> None:
    screen = ConfiguredScreen.from_dict(
        {
            "id": "screen-1",
            "name": "Front TV",
            "transport": "browser",
            "binding": {"clientId": "abc"},
        }
    )

    assert screen.capabilities == list(supported_capabilities("browser"))
    assert screen.to_dict() == {
        "id": "screen-1",
        "name": "Front TV",
        "transport": "browser",
        "capabilities": list(supported_capabilities("browser")),
        "binding": {"clientId": "abc"},
    }


def test_configured_screen_validation_rejects_missing_name() -> None:
    with pytest.raises(ValueError, match="must have a name"):
        ConfiguredScreen.from_dict({"id": "screen-2", "transport": "browser", "capabilities": ["static_media"]})


def test_configured_screen_validation_rejects_unknown_transport() -> None:
    with pytest.raises(ValueError, match="valid screen transport"):
        ConfiguredScreen.from_dict({"id": "screen-3", "name": "Display", "transport": "satellite"})


def test_configured_screen_validation_rejects_unsupported_capability() -> None:
    with pytest.raises(ValueError, match="does not support"):
        ConfiguredScreen.from_dict(
            {
                "id": "screen-4",
                "name": "LAN TV",
                "transport": "browser",
                "capabilities": ["static_media", "remote_input"],
            }
        )


def test_configured_screen_validate_rejects_blank_id() -> None:
    screen = ConfiguredScreen(
        id=" ",
        name="Lobby",
        transport="browser",
        capabilities=list(supported_capabilities("browser")),
    )
    with pytest.raises(ValueError, match="must have an id"):
        screen.validate()


def test_parse_configured_screens_skips_invalid_and_duplicate_entries() -> None:
    screens = parse_configured_screens(
        [
            {
                "id": "screen-1",
                "name": "Browser TV",
                "transport": "browser",
                "capabilities": ["static_media"],
            },
            {
                "id": "screen-1",
                "name": "Duplicate",
                "transport": "browser",
                "capabilities": ["static_media"],
            },
            {
                "id": "screen-2",
                "name": "Broken Browser",
                "transport": "browser",
                "capabilities": ["remote_input"],
            },
            "not-a-dict",
        ]
    )

    assert [screen.id for screen in screens] == ["screen-1"]
    assert screens[0].transport == "browser"
    assert TRANSPORT_LABELS[screens[0].transport] == "Browser Receiver"
    assert CAPABILITY_LABELS[screens[0].capabilities[0]] == "Images / Videos"


def test_parse_configured_screens_returns_empty_for_non_lists() -> None:
    assert parse_configured_screens(None) == []


def test_validate_configured_screens_rejects_non_list_payloads() -> None:
    with pytest.raises(ValueError, match="must be a list"):
        validate_configured_screens({"id": "screen-1"})


def test_validate_configured_screens_rejects_non_object_entries() -> None:
    with pytest.raises(ValueError, match="must be an object"):
        validate_configured_screens(["bad"])


def test_validate_configured_screens_rejects_duplicate_ids() -> None:
    with pytest.raises(ValueError, match="must be unique"):
        validate_configured_screens(
            [
                {"id": "screen-1", "name": "A", "transport": "browser", "capabilities": ["static_media"]},
                {"id": "screen-1", "name": "B", "transport": "browser", "capabilities": ["static_media"]},
            ]
        )


def test_validate_configured_screens_accepts_empty_lists() -> None:
    assert validate_configured_screens([]) == []
