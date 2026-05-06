from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Any, Mapping
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

from screen_protocols import ConfiguredScreen


DLNA_BINDING_KEY = "dlna_device_usn"
DLNA_AVTRANSPORT_SERVICE = "urn:schemas-upnp-org:service:AVTransport:1"
DLNA_RENDERING_SERVICE = "urn:schemas-upnp-org:service:RenderingControl:1"


@dataclass
class DlnaDevice:
    usn: str
    location: str
    friendly_name: str
    av_transport_url: str
    rendering_control_url: str = ""
    presentation_url: str = ""
    manufacturer: str = ""
    model_name: str = ""
    ip: str = ""
    device_type: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "usn": self.usn,
            "location": self.location,
            "friendly_name": self.friendly_name,
            "av_transport_url": self.av_transport_url,
            "rendering_control_url": self.rendering_control_url,
            "presentation_url": self.presentation_url,
            "manufacturer": self.manufacturer,
            "model_name": self.model_name,
            "ip": self.ip,
            "device_type": self.device_type,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "DlnaDevice":
        device = cls(
            usn=str(data.get("usn") or "").strip(),
            location=str(data.get("location") or "").strip(),
            friendly_name=str(data.get("friendly_name") or "").strip(),
            av_transport_url=str(data.get("av_transport_url") or "").strip(),
            rendering_control_url=str(data.get("rendering_control_url") or "").strip(),
            presentation_url=str(data.get("presentation_url") or "").strip(),
            manufacturer=str(data.get("manufacturer") or "").strip(),
            model_name=str(data.get("model_name") or "").strip(),
            ip=str(data.get("ip") or "").strip(),
            device_type=str(data.get("device_type") or "").strip(),
        )
        if not device.usn or not device.location or not device.friendly_name or not device.av_transport_url:
            raise ValueError("DLNA devices must include usn, location, friendly_name, and av_transport_url.")
        return device


def parse_ssdp_response(payload: bytes) -> dict[str, str]:
    try:
        text = payload.decode("utf-8", errors="ignore")
    except Exception:
        return {}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().upper()] = value.strip()
    return headers


def is_dlna_media_renderer(headers: Mapping[str, str]) -> bool:
    st = str(headers.get("ST") or headers.get("NT") or "").lower()
    usn = str(headers.get("USN") or "").lower()
    server = str(headers.get("SERVER") or "").lower()
    return "mediarenderer" in st or "mediarenderer" in usn or "dlna" in server


def absolutize_url(base_url: str, candidate: str) -> str:
    return urljoin(base_url, candidate.strip())


def _text(element: ET.Element | None) -> str:
    return (element.text or "").strip() if element is not None else ""


def parse_device_description(location_url: str, xml_text: str, sender_ip: str = "") -> DlnaDevice | None:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    device = root.find(".//{*}device")
    if device is None:
        return None
    device_type = _text(device.find("{*}deviceType"))
    if "MediaRenderer" not in device_type:
        return None
    friendly_name = _text(device.find("{*}friendlyName"))
    manufacturer = _text(device.find("{*}manufacturer"))
    model_name = _text(device.find("{*}modelName"))
    presentation_url = absolutize_url(location_url, _text(device.find("{*}presentationURL"))) if _text(device.find("{*}presentationURL")) else ""
    usn = _text(device.find("{*}UDN"))
    if not usn:
        return None

    av_transport_url = ""
    rendering_control_url = ""
    for service in device.findall(".//{*}service"):
        service_type = _text(service.find("{*}serviceType"))
        control_url = _text(service.find("{*}controlURL"))
        if not control_url:
            continue
        absolute_control_url = absolutize_url(location_url, control_url)
        if service_type == DLNA_AVTRANSPORT_SERVICE:
            av_transport_url = absolute_control_url
        if service_type == DLNA_RENDERING_SERVICE:
            rendering_control_url = absolute_control_url
    if not friendly_name or not av_transport_url:
        return None
    return DlnaDevice(
        usn=usn,
        location=location_url,
        friendly_name=friendly_name,
        av_transport_url=av_transport_url,
        rendering_control_url=rendering_control_url,
        presentation_url=presentation_url,
        manufacturer=manufacturer,
        model_name=model_name,
        ip=sender_ip or urlparse(location_url).hostname or "",
        device_type=device_type,
    )


def dlna_binding_usn(screen: ConfiguredScreen) -> str:
    if screen.transport != "dlna":
        return ""
    binding = screen.binding or {}
    explicit = str(binding.get(DLNA_BINDING_KEY) or "").strip()
    if explicit:
        return explicit
    return str(binding.get("value") or "").strip()


def validate_unique_dlna_bindings(configured_screens: list[ConfiguredScreen]) -> None:
    seen: dict[str, str] = {}
    for screen in configured_screens:
        usn = dlna_binding_usn(screen)
        if not usn:
            continue
        previous = seen.get(usn)
        if previous is not None:
            raise ValueError("Each discovered DLNA device can only be bound to one configured DLNA screen.")
        seen[usn] = screen.id


def bound_dlna_device(screen: ConfiguredScreen, devices_by_usn: Mapping[str, Mapping[str, Any]]) -> dict[str, Any] | None:
    usn = dlna_binding_usn(screen)
    if not usn:
        return None
    device = devices_by_usn.get(usn)
    return dict(device) if isinstance(device, Mapping) else None


def dlna_target_status_detail(screen: ConfiguredScreen, devices_by_usn: Mapping[str, Mapping[str, Any]]) -> tuple[bool, str, str]:
    usn = dlna_binding_usn(screen)
    if not usn:
        return False, "DLNA renderer • Unbound", "Choose a discovered DLNA renderer for this screen."
    device = bound_dlna_device(screen, devices_by_usn)
    if device is None:
        return False, f"DLNA renderer • Bound to {usn}", "The bound DLNA renderer is not currently discovered."
    name = str(device.get("friendly_name") or usn)
    ip = str(device.get("ip") or "").strip()
    detail = f"DLNA renderer • {name}"
    if ip:
        detail += f" • {ip}"
    return True, detail, ""


def resolve_dlna_commands(
    configured_screens: list[ConfiguredScreen],
    devices_by_usn: Mapping[str, Mapping[str, Any]],
    screen_commands: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    resolved: dict[str, dict[str, Any]] = {}
    screens_by_id = {screen.id: screen for screen in configured_screens if screen.transport == "dlna"}
    for screen_id, command in screen_commands.items():
        screen = screens_by_id.get(screen_id)
        if screen is None:
            continue
        usn = dlna_binding_usn(screen)
        if not usn or usn not in devices_by_usn:
            continue
        translated = dict(command)
        translated["dlnaUsn"] = usn
        translated["configuredScreenId"] = screen.id
        translated["configuredScreenName"] = screen.name
        resolved[usn] = translated
    return resolved


def build_didl_lite_metadata(title: str, media_url: str, mime_type: str) -> str:
    safe_title = escape(title or "Background Screen Media")
    safe_url = escape(media_url)
    safe_mime = escape(mime_type or "application/octet-stream")
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="0" parentID="-1" restricted="1">'
        f"<dc:title>{safe_title}</dc:title>"
        "<upnp:class>object.item.videoItem</upnp:class>"
        f'<res protocolInfo="http-get:*:{safe_mime}:DLNA.ORG_OP=01">{safe_url}</res>'
        "</item>"
        "</DIDL-Lite>"
    )


def build_soap_envelope(service_type: str, action: str, arguments: Mapping[str, object]) -> str:
    args_xml = "".join(f"<{key}>{escape(str(value))}</{key}>" for key, value in arguments.items())
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        f'<u:{action} xmlns:u="{service_type}">{args_xml}</u:{action}>'
        "</s:Body>"
        "</s:Envelope>"
    )
