from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from screen_protocols import ConfiguredScreen


MIRACAST_BINDING_KEY = "miracast_device_id"


@dataclass
class MiracastDevice:
    device_id: str
    friendly_name: str
    status: str
    class_name: str
    instance_id: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "device_id": self.device_id,
            "friendly_name": self.friendly_name,
            "status": self.status,
            "class_name": self.class_name,
            "instance_id": self.instance_id,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "MiracastDevice":
        device = cls(
            device_id=str(data.get("device_id") or "").strip(),
            friendly_name=str(data.get("friendly_name") or "").strip(),
            status=str(data.get("status") or "").strip(),
            class_name=str(data.get("class_name") or "").strip(),
            instance_id=str(data.get("instance_id") or "").strip(),
            detail=str(data.get("detail") or "").strip(),
        )
        if not device.device_id or not device.friendly_name:
            raise ValueError("Miracast devices must include device_id and friendly_name.")
        return device


def parse_pnp_devices_json(text: str) -> list[dict[str, str]]:
    cleaned = text.strip()
    if not cleaned:
        return []
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return []
    devices: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        devices.append(
            {
                "status": str(item.get("Status") or item.get("status") or "").strip(),
                "class_name": str(item.get("Class") or item.get("class_name") or "").strip(),
                "friendly_name": str(item.get("FriendlyName") or item.get("friendly_name") or "").strip(),
                "instance_id": str(item.get("InstanceId") or item.get("instance_id") or "").strip(),
            }
        )
    return devices


def is_miracast_candidate(device: Mapping[str, str]) -> bool:
    friendly = str(device.get("friendly_name") or "").lower()
    class_name = str(device.get("class_name") or "").lower()
    instance_id = str(device.get("instance_id") or "").lower()
    haystack = " ".join([friendly, class_name, instance_id])
    positive_terms = ("miracast", "wireless display", "cast", "project", "wfd")
    negative_terms = ("intel(", "nvidia", "generic monitor", "display audio", "u hd graphics", "quadro", "integrated monitor")
    if any(term in haystack for term in positive_terms):
        return True
    if any(term in haystack for term in negative_terms):
        return False
    return class_name in {"softwaredevice", "monitor"} and ("wireless" in haystack or "display" in haystack)


def miracast_device_from_pnp(device: Mapping[str, str]) -> MiracastDevice | None:
    if not is_miracast_candidate(device):
        return None
    friendly_name = str(device.get("friendly_name") or "").strip()
    instance_id = str(device.get("instance_id") or "").strip()
    class_name = str(device.get("class_name") or "").strip()
    status = str(device.get("status") or "").strip() or "Unknown"
    if not friendly_name or not instance_id:
        return None
    detail = f"{class_name} • {status}" if class_name else status
    return MiracastDevice(
        device_id=instance_id,
        friendly_name=friendly_name,
        status=status,
        class_name=class_name,
        instance_id=instance_id,
        detail=detail,
    )


def parse_miracast_devices_from_pnp_json(text: str) -> list[MiracastDevice]:
    devices: list[MiracastDevice] = []
    seen_ids: set[str] = set()
    for item in parse_pnp_devices_json(text):
        device = miracast_device_from_pnp(item)
        if device is None or device.device_id in seen_ids:
            continue
        seen_ids.add(device.device_id)
        devices.append(device)
    return devices


def miracast_binding_device_id(screen: ConfiguredScreen) -> str:
    if screen.transport != "miracast":
        return ""
    binding = screen.binding or {}
    explicit = str(binding.get(MIRACAST_BINDING_KEY) or "").strip()
    if explicit:
        return explicit
    return str(binding.get("value") or "").strip()


def validate_unique_miracast_bindings(configured_screens: list[ConfiguredScreen]) -> None:
    seen: dict[str, str] = {}
    for screen in configured_screens:
        device_id = miracast_binding_device_id(screen)
        if not device_id:
            continue
        previous = seen.get(device_id)
        if previous is not None:
            raise ValueError("Each discovered Miracast device can only be bound to one configured Miracast screen.")
        seen[device_id] = screen.id


def bound_miracast_device(screen: ConfiguredScreen, devices_by_id: Mapping[str, Mapping[str, Any]]) -> dict[str, Any] | None:
    device_id = miracast_binding_device_id(screen)
    if not device_id:
        return None
    device = devices_by_id.get(device_id)
    return dict(device) if isinstance(device, Mapping) else None


def miracast_target_status_detail(screen: ConfiguredScreen, devices_by_id: Mapping[str, Mapping[str, Any]]) -> tuple[bool, str, str]:
    device_id = miracast_binding_device_id(screen)
    if not device_id:
        return False, "Miracast display • Unbound", "Choose a discovered Miracast device for this screen."
    device = bound_miracast_device(screen, devices_by_id)
    if device is None:
        return False, f"Miracast display • Bound to {device_id}", "The bound Miracast device is not currently discovered."
    name = str(device.get("friendly_name") or device_id)
    detail = f"Miracast display • {name}"
    extra = str(device.get("detail") or "").strip()
    if extra:
        detail += f" • {extra}"
    return True, detail, ""
