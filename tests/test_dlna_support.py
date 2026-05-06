from __future__ import annotations

import pytest

from dlna_support import (
    DLNA_AVTRANSPORT_SERVICE,
    DLNA_BINDING_KEY,
    DLNA_RENDERING_SERVICE,
    DlnaDevice,
    absolutize_url,
    bound_dlna_device,
    build_didl_lite_metadata,
    build_soap_envelope,
    dlna_binding_usn,
    dlna_target_status_detail,
    is_dlna_media_renderer,
    parse_device_description,
    parse_ssdp_response,
    resolve_dlna_commands,
    validate_unique_dlna_bindings,
)
from screen_protocols import ConfiguredScreen, supported_capabilities


def make_dlna_screen(screen_id: str, name: str, binding: dict[str, str] | None = None) -> ConfiguredScreen:
    return ConfiguredScreen(
        id=screen_id,
        name=name,
        transport="dlna",
        capabilities=list(supported_capabilities("dlna")),
        binding=binding or {},
    )


def test_dlna_device_round_trips() -> None:
    device = DlnaDevice(
        usn="uuid:abc",
        location="http://192.168.0.10:1234/device.xml",
        friendly_name="Living Room TV",
        av_transport_url="http://192.168.0.10:1234/upnp/control/avtransport1",
    )
    assert DlnaDevice.from_dict(device.to_dict()).to_dict() == device.to_dict()


def test_dlna_device_requires_core_fields() -> None:
    with pytest.raises(ValueError, match="must include"):
        DlnaDevice.from_dict({"usn": "uuid:abc"})


def test_parse_ssdp_response_extracts_headers() -> None:
    payload = (
        b"HTTP/1.1 200 OK\r\n"
        b"CACHE-CONTROL: max-age=1800\r\n"
        b"ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
        b"USN: uuid:abc::urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
        b"LOCATION: http://192.168.0.10:1234/device.xml\r\n\r\n"
    )
    headers = parse_ssdp_response(payload)
    assert headers["ST"] == "urn:schemas-upnp-org:device:MediaRenderer:1"
    assert headers["USN"].startswith("uuid:abc")


def test_parse_ssdp_response_returns_empty_when_decode_fails() -> None:
    class BadPayload:
        def decode(self, *_args, **_kwargs):
            raise UnicodeError("bad")

    assert parse_ssdp_response(BadPayload()) == {}


def test_parse_ssdp_response_ignores_lines_without_colons() -> None:
    payload = b"HTTP/1.1 200 OK\r\nBROKEN-LINE\r\nUSN: uuid:abc\r\n\r\n"
    assert parse_ssdp_response(payload) == {"USN": "uuid:abc"}


def test_is_dlna_media_renderer_detects_common_signals() -> None:
    assert is_dlna_media_renderer({"ST": "urn:schemas-upnp-org:device:MediaRenderer:1"}) is True
    assert is_dlna_media_renderer({"USN": "uuid:abc::urn:schemas-upnp-org:device:MediaRenderer:1"}) is True
    assert is_dlna_media_renderer({"SERVER": "DLNADOC/1.50 UPnP/1.0"}) is True
    assert is_dlna_media_renderer({"ST": "urn:schemas-upnp-org:device:MediaServer:1"}) is False


def test_absolutize_url_joins_relative_paths() -> None:
    assert absolutize_url("http://192.168.0.10:1234/device.xml", "/upnp/control") == "http://192.168.0.10:1234/upnp/control"


def test_parse_device_description_extracts_renderer_and_service_urls() -> None:
    xml_text = f"""
    <root xmlns="urn:schemas-upnp-org:device-1-0">
      <device>
        <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
        <friendlyName>Living Room TV</friendlyName>
        <manufacturer>ACME</manufacturer>
        <modelName>Model X</modelName>
        <UDN>uuid:abc</UDN>
        <presentationURL>/present</presentationURL>
        <serviceList>
          <service>
            <serviceType>{DLNA_AVTRANSPORT_SERVICE}</serviceType>
            <controlURL>/upnp/control/avtransport1</controlURL>
          </service>
          <service>
            <serviceType>{DLNA_RENDERING_SERVICE}</serviceType>
            <controlURL>/upnp/control/rendering1</controlURL>
          </service>
        </serviceList>
      </device>
    </root>
    """
    device = parse_device_description("http://192.168.0.10:1234/device.xml", xml_text, "192.168.0.10")
    assert device is not None
    assert device.friendly_name == "Living Room TV"
    assert device.av_transport_url == "http://192.168.0.10:1234/upnp/control/avtransport1"
    assert device.rendering_control_url == "http://192.168.0.10:1234/upnp/control/rendering1"
    assert device.presentation_url == "http://192.168.0.10:1234/present"


def test_parse_device_description_rejects_non_renderers_or_broken_xml() -> None:
    assert parse_device_description("http://device.xml", "<broken>", "192.168.0.10") is None
    assert parse_device_description("http://device.xml", "<root />", "192.168.0.10") is None
    xml_text = """
    <root xmlns="urn:schemas-upnp-org:device-1-0">
      <device>
        <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
      </device>
    </root>
    """
    assert parse_device_description("http://device.xml", xml_text, "192.168.0.10") is None


def test_parse_device_description_rejects_missing_udn_or_transport_service() -> None:
    missing_udn = f"""
    <root xmlns="urn:schemas-upnp-org:device-1-0">
      <device>
        <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
        <friendlyName>Living Room TV</friendlyName>
        <serviceList>
          <service>
            <serviceType>{DLNA_AVTRANSPORT_SERVICE}</serviceType>
            <controlURL>/upnp/control/avtransport1</controlURL>
          </service>
        </serviceList>
      </device>
    </root>
    """
    assert parse_device_description("http://device.xml", missing_udn, "192.168.0.10") is None

    missing_transport = """
    <root xmlns="urn:schemas-upnp-org:device-1-0">
      <device>
        <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
        <friendlyName>Living Room TV</friendlyName>
        <UDN>uuid:abc</UDN>
        <serviceList>
          <service>
            <serviceType>urn:schemas-upnp-org:service:Other:1</serviceType>
            <controlURL></controlURL>
          </service>
        </serviceList>
      </device>
    </root>
    """
    assert parse_device_description("http://device.xml", missing_transport, "192.168.0.10") is None

    rendering_only = f"""
    <root xmlns="urn:schemas-upnp-org:device-1-0">
      <device>
        <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
        <friendlyName>Living Room TV</friendlyName>
        <UDN>uuid:abc</UDN>
        <serviceList>
          <service>
            <serviceType>{DLNA_RENDERING_SERVICE}</serviceType>
            <controlURL>/upnp/control/rendering1</controlURL>
          </service>
        </serviceList>
      </device>
    </root>
    """
    assert parse_device_description("http://device.xml", rendering_only, "192.168.0.10") is None


def test_dlna_binding_usn_reads_explicit_or_legacy_binding() -> None:
    assert dlna_binding_usn(make_dlna_screen("screen-1", "TV", {DLNA_BINDING_KEY: "uuid:abc"})) == "uuid:abc"
    assert dlna_binding_usn(make_dlna_screen("screen-1", "TV", {"value": "uuid:legacy"})) == "uuid:legacy"
    non_dlna = ConfiguredScreen(
        id="screen-2",
        name="Browser",
        transport="browser",
        capabilities=list(supported_capabilities("browser")),
        binding={DLNA_BINDING_KEY: "uuid:abc"},
    )
    assert dlna_binding_usn(non_dlna) == ""


def test_validate_unique_dlna_bindings_rejects_duplicates() -> None:
    with pytest.raises(ValueError, match="only be bound to one"):
        validate_unique_dlna_bindings(
            [
                make_dlna_screen("screen-1", "TV 1", {DLNA_BINDING_KEY: "uuid:abc"}),
                make_dlna_screen("screen-2", "TV 2", {DLNA_BINDING_KEY: "uuid:abc"}),
            ]
        )


def test_validate_unique_dlna_bindings_allows_unique_and_unbound() -> None:
    validate_unique_dlna_bindings(
        [
            make_dlna_screen("screen-1", "TV 1", {DLNA_BINDING_KEY: "uuid:abc"}),
            make_dlna_screen("screen-2", "TV 2"),
        ]
    )


def test_bound_dlna_device_returns_copy_or_none() -> None:
    state = bound_dlna_device(
        make_dlna_screen("screen-1", "TV", {DLNA_BINDING_KEY: "uuid:abc"}),
        {"uuid:abc": {"friendly_name": "TV"}},
    )
    assert state == {"friendly_name": "TV"}
    assert bound_dlna_device(make_dlna_screen("screen-2", "TV"), {}) is None


def test_dlna_target_status_detail_reports_binding_state() -> None:
    online, detail, warning = dlna_target_status_detail(make_dlna_screen("screen-1", "TV"), {})
    assert online is False
    assert detail == "DLNA renderer • Unbound"
    assert "Choose a discovered DLNA renderer" in warning

    online, detail, warning = dlna_target_status_detail(
        make_dlna_screen("screen-1", "TV", {DLNA_BINDING_KEY: "uuid:abc"}),
        {},
    )
    assert online is False
    assert detail == "DLNA renderer • Bound to uuid:abc"
    assert "not currently discovered" in warning

    online, detail, warning = dlna_target_status_detail(
        make_dlna_screen("screen-1", "TV", {DLNA_BINDING_KEY: "uuid:abc"}),
        {"uuid:abc": {"friendly_name": "Living Room TV", "ip": "192.168.0.10"}},
    )
    assert online is True
    assert detail == "DLNA renderer • Living Room TV • 192.168.0.10"
    assert warning == ""


def test_dlna_target_status_detail_without_ip_keeps_name_only() -> None:
    online, detail, warning = dlna_target_status_detail(
        make_dlna_screen("screen-1", "TV", {DLNA_BINDING_KEY: "uuid:abc"}),
        {"uuid:abc": {"friendly_name": "Living Room TV"}},
    )
    assert online is True
    assert detail == "DLNA renderer • Living Room TV"
    assert warning == ""


def test_resolve_dlna_commands_maps_configured_screens_to_bound_devices() -> None:
    resolved = resolve_dlna_commands(
        [make_dlna_screen("screen-1", "TV", {DLNA_BINDING_KEY: "uuid:abc"})],
        {"uuid:abc": {"friendly_name": "TV"}},
        {"screen-1": {"type": "play", "mediaUrl": "http://example.com/video.mp4"}},
    )
    assert resolved == {
        "uuid:abc": {
            "type": "play",
            "mediaUrl": "http://example.com/video.mp4",
            "dlnaUsn": "uuid:abc",
            "configuredScreenId": "screen-1",
            "configuredScreenName": "TV",
        }
    }


def test_resolve_dlna_commands_ignores_unbound_or_unknown_devices() -> None:
    assert resolve_dlna_commands(
        [make_dlna_screen("screen-1", "TV")],
        {},
        {"screen-1": {"type": "play"}},
    ) == {}


def test_resolve_dlna_commands_ignores_non_dlna_screen_entries() -> None:
    non_dlna = ConfiguredScreen(
        id="screen-2",
        name="Browser",
        transport="browser",
        capabilities=list(supported_capabilities("browser")),
    )
    assert resolve_dlna_commands([non_dlna], {"uuid:abc": {"friendly_name": "TV"}}, {"screen-2": {"type": "play"}}) == {}


def test_build_didl_lite_metadata_includes_title_url_and_mime() -> None:
    metadata = build_didl_lite_metadata("Demo", "http://example.com/video.mp4", "video/mp4")
    assert "Demo" in metadata
    assert "http://example.com/video.mp4" in metadata
    assert "video/mp4" in metadata


def test_build_soap_envelope_includes_service_action_and_arguments() -> None:
    envelope = build_soap_envelope(DLNA_AVTRANSPORT_SERVICE, "Play", {"InstanceID": 0, "Speed": 1})
    assert DLNA_AVTRANSPORT_SERVICE in envelope
    assert "<u:Play" in envelope
    assert "<InstanceID>0</InstanceID>" in envelope
    assert "<Speed>1</Speed>" in envelope
