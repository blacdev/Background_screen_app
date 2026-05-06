from __future__ import annotations

import pytest

from miracast_support import (
    MIRACAST_BINDING_KEY,
    MiracastDevice,
    bound_miracast_device,
    is_miracast_candidate,
    miracast_binding_device_id,
    miracast_device_from_pnp,
    miracast_target_status_detail,
    parse_miracast_devices_from_pnp_json,
    parse_pnp_devices_json,
    validate_unique_miracast_bindings,
)
from screen_protocols import ConfiguredScreen, supported_capabilities


def make_miracast_screen(screen_id: str, name: str, binding: dict[str, str] | None = None) -> ConfiguredScreen:
    return ConfiguredScreen(
        id=screen_id,
        name=name,
        transport="miracast",
        capabilities=list(supported_capabilities("miracast")),
        binding=binding or {},
    )


def test_miracast_device_round_trips() -> None:
    device = MiracastDevice(
        device_id="SWD\\MIRACAST\\DEVICE1",
        friendly_name="Office TV",
        status="OK",
        class_name="SoftwareDevice",
        instance_id="SWD\\MIRACAST\\DEVICE1",
        detail="SoftwareDevice • OK",
    )
    assert MiracastDevice.from_dict(device.to_dict()).to_dict() == device.to_dict()


def test_miracast_device_requires_id_and_name() -> None:
    with pytest.raises(ValueError, match="must include"):
        MiracastDevice.from_dict({"device_id": ""})


def test_parse_pnp_devices_json_accepts_single_object_list_and_invalid_text() -> None:
    single = '{"Status":"OK","Class":"SoftwareDevice","FriendlyName":"Office TV","InstanceId":"SWD\\\\MIRACAST\\\\DEVICE1"}'
    assert parse_pnp_devices_json(single) == [
        {
            "status": "OK",
            "class_name": "SoftwareDevice",
            "friendly_name": "Office TV",
            "instance_id": r"SWD\MIRACAST\DEVICE1",
        }
    ]
    assert parse_pnp_devices_json("not-json") == []
    assert parse_pnp_devices_json("") == []
    assert parse_pnp_devices_json("5") == []


def test_parse_pnp_devices_json_skips_non_mapping_entries() -> None:
    assert parse_pnp_devices_json('[1, {"FriendlyName":"TV","InstanceId":"X"}]') == [
        {
            "status": "",
            "class_name": "",
            "friendly_name": "TV",
            "instance_id": "X",
        }
    ]


def test_is_miracast_candidate_matches_wireless_display_and_rejects_local_monitors() -> None:
    assert is_miracast_candidate(
        {
            "friendly_name": "Living Room Wireless Display",
            "class_name": "SoftwareDevice",
            "instance_id": r"SWD\MIRACAST\DEVICE1",
        }
    ) is True
    assert is_miracast_candidate(
        {
            "friendly_name": "Generic Monitor",
            "class_name": "Monitor",
            "instance_id": r"DISPLAY\AAA0000",
        }
    ) is False


def test_miracast_device_from_pnp_creates_devices_for_candidates() -> None:
    device = miracast_device_from_pnp(
        {
            "friendly_name": "Living Room Wireless Display",
            "class_name": "SoftwareDevice",
            "instance_id": r"SWD\MIRACAST\DEVICE1",
            "status": "OK",
        }
    )
    assert device is not None
    assert device.friendly_name == "Living Room Wireless Display"
    assert miracast_device_from_pnp({"friendly_name": "Generic Monitor", "class_name": "Monitor", "instance_id": r"DISPLAY\AAA"}) is None
    assert miracast_device_from_pnp({"friendly_name": "", "class_name": "SoftwareDevice", "instance_id": ""}) is None
    assert miracast_device_from_pnp({"friendly_name": "Wireless Display", "class_name": "SoftwareDevice", "instance_id": ""}) is None


def test_parse_miracast_devices_from_pnp_json_filters_duplicates_and_non_candidates() -> None:
    payload = r"""
    [
      {"Status":"OK","Class":"SoftwareDevice","FriendlyName":"Office TV","InstanceId":"SWD\\MIRACAST\\DEVICE1"},
      {"Status":"OK","Class":"SoftwareDevice","FriendlyName":"Office TV","InstanceId":"SWD\\MIRACAST\\DEVICE1"},
      {"Status":"OK","Class":"Monitor","FriendlyName":"Generic Monitor","InstanceId":"DISPLAY\\AAA0000"}
    ]
    """
    devices = parse_miracast_devices_from_pnp_json(payload)
    assert [device.device_id for device in devices] == [r"SWD\MIRACAST\DEVICE1"]


def test_miracast_binding_device_id_reads_explicit_or_legacy_binding() -> None:
    assert miracast_binding_device_id(make_miracast_screen("screen-1", "TV", {MIRACAST_BINDING_KEY: "SWD\\MIRACAST\\DEVICE1"})) == r"SWD\MIRACAST\DEVICE1"
    assert miracast_binding_device_id(make_miracast_screen("screen-1", "TV", {"value": "LEGACY"})) == "LEGACY"
    non_miracast = ConfiguredScreen(
        id="screen-2",
        name="Browser",
        transport="browser",
        capabilities=list(supported_capabilities("browser")),
        binding={MIRACAST_BINDING_KEY: "X"},
    )
    assert miracast_binding_device_id(non_miracast) == ""


def test_validate_unique_miracast_bindings_rejects_duplicates() -> None:
    with pytest.raises(ValueError, match="only be bound to one"):
        validate_unique_miracast_bindings(
            [
                make_miracast_screen("screen-1", "TV 1", {MIRACAST_BINDING_KEY: "DEVICE"}),
                make_miracast_screen("screen-2", "TV 2", {MIRACAST_BINDING_KEY: "DEVICE"}),
            ]
        )


def test_validate_unique_miracast_bindings_allows_unique_and_unbound() -> None:
    validate_unique_miracast_bindings(
        [
            make_miracast_screen("screen-1", "TV 1", {MIRACAST_BINDING_KEY: "DEVICE-1"}),
            make_miracast_screen("screen-2", "TV 2"),
        ]
    )


def test_bound_miracast_device_returns_copy_or_none() -> None:
    state = bound_miracast_device(
        make_miracast_screen("screen-1", "TV", {MIRACAST_BINDING_KEY: "DEVICE"}),
        {"DEVICE": {"friendly_name": "TV"}},
    )
    assert state == {"friendly_name": "TV"}
    assert bound_miracast_device(make_miracast_screen("screen-2", "TV"), {}) is None


def test_miracast_target_status_detail_reports_binding_state() -> None:
    online, detail, warning = miracast_target_status_detail(make_miracast_screen("screen-1", "TV"), {})
    assert online is False
    assert detail == "Miracast display • Unbound"
    assert "Choose a discovered Miracast device" in warning

    online, detail, warning = miracast_target_status_detail(
        make_miracast_screen("screen-1", "TV", {MIRACAST_BINDING_KEY: "DEVICE"}),
        {},
    )
    assert online is False
    assert detail == "Miracast display • Bound to DEVICE"
    assert "not currently discovered" in warning

    online, detail, warning = miracast_target_status_detail(
        make_miracast_screen("screen-1", "TV", {MIRACAST_BINDING_KEY: "DEVICE"}),
        {"DEVICE": {"friendly_name": "Office TV", "detail": "SoftwareDevice • OK"}},
    )
    assert online is True
    assert detail == "Miracast display • Office TV • SoftwareDevice • OK"
    assert warning == ""


def test_miracast_target_status_detail_without_extra_detail_keeps_name_only() -> None:
    online, detail, warning = miracast_target_status_detail(
        make_miracast_screen("screen-1", "TV", {MIRACAST_BINDING_KEY: "DEVICE"}),
        {"DEVICE": {"friendly_name": "Office TV"}},
    )
    assert online is True
    assert detail == "Miracast display • Office TV"
    assert warning == ""
