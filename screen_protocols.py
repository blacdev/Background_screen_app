from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Iterable, Mapping


SCREEN_TRANSPORTS = ("local_hdmi", "browser", "dlna", "miracast")
SCREEN_CAPABILITIES = ("static_media", "playlist", "live_view", "remote_input", "sync_playback")

TRANSPORT_LABELS = {
    "local_hdmi": "Local HDMI",
    "browser": "Browser Receiver",
    "dlna": "DLNA",
    "miracast": "Miracast",
}

CAPABILITY_LABELS = {
    "static_media": "Images / Videos",
    "playlist": "Sequential Playback",
    "live_view": "Live App / Display View",
    "remote_input": "Remote Input",
    "sync_playback": "Sync Playback",
}

TRANSPORT_CAPABILITY_MATRIX = {
    "local_hdmi": frozenset({"static_media", "playlist", "live_view", "sync_playback"}),
    "browser": frozenset({"static_media", "playlist", "live_view", "sync_playback"}),
    "dlna": frozenset({"static_media", "playlist"}),
    "miracast": frozenset({"live_view"}),
}


def normalize_transport(value: object) -> str:
    cleaned = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "local": "local_hdmi",
        "local_display": "local_hdmi",
        "hdmi": "local_hdmi",
        "local_hdmi": "local_hdmi",
        "browser": "browser",
        "browser_receiver": "browser",
        "web": "browser",
        "dlna": "dlna",
        "miracast": "miracast",
    }
    return aliases.get(cleaned, "")


def normalize_capability(value: object) -> str:
    cleaned = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "media": "static_media",
        "static_media": "static_media",
        "image_video": "static_media",
        "playlist": "playlist",
        "sequential_playback": "playlist",
        "live_view": "live_view",
        "live_stream": "live_view",
        "remote_input": "remote_input",
        "input": "remote_input",
        "sync": "sync_playback",
        "sync_playback": "sync_playback",
    }
    return aliases.get(cleaned, "")


def supported_capabilities(transport: str) -> tuple[str, ...]:
    normalized = normalize_transport(transport)
    if not normalized:
        return ()
    return tuple(capability for capability in SCREEN_CAPABILITIES if capability in TRANSPORT_CAPABILITY_MATRIX[normalized])


def normalized_capabilities(values: Iterable[object] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values or []:
        capability = normalize_capability(item)
        if not capability or capability in seen:
            continue
        seen.add(capability)
        normalized.append(capability)
    return normalized


def unsupported_capabilities(transport: str, capabilities: Iterable[object] | None) -> list[str]:
    supported = set(supported_capabilities(transport))
    requested = normalized_capabilities(capabilities)
    return [capability for capability in requested if capability not in supported]


@dataclass
class ConfiguredScreen:
    id: str
    name: str
    transport: str
    capabilities: list[str] = field(default_factory=list)
    binding: dict[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.id.strip():
            raise ValueError("Configured screens must have an id.")
        if not self.name.strip():
            raise ValueError("Configured screens must have a name.")
        if self.transport not in SCREEN_TRANSPORTS:
            raise ValueError("Choose a valid screen transport.")
        unsupported = unsupported_capabilities(self.transport, self.capabilities)
        if unsupported:
            names = ", ".join(CAPABILITY_LABELS.get(item, item) for item in unsupported)
            raise ValueError(f"{TRANSPORT_LABELS[self.transport]} does not support: {names}.")

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "id": self.id,
            "name": self.name,
            "transport": self.transport,
            "capabilities": self.capabilities[:],
            "binding": dict(self.binding),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ConfiguredScreen":
        raw_binding = data.get("binding")
        binding: dict[str, str] = {}
        if isinstance(raw_binding, Mapping):
            binding = {
                str(key): str(value)
                for key, value in raw_binding.items()
                if isinstance(key, str)
                if isinstance(value, str)
            }
        transport = normalize_transport(data.get("transport"))
        capabilities = normalized_capabilities(data.get("capabilities"))
        if transport and not capabilities:
            capabilities = list(supported_capabilities(transport))
        screen = cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            name=str(data.get("name") or "").strip(),
            transport=transport,
            capabilities=capabilities,
            binding=binding,
        )
        screen.validate()
        return screen


def parse_configured_screens(raw_items: object) -> list[ConfiguredScreen]:
    if not isinstance(raw_items, list):
        return []
    screens: list[ConfiguredScreen] = []
    seen_ids: set[str] = set()
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        try:
            screen = ConfiguredScreen.from_dict(item)
        except ValueError:
            continue
        if screen.id in seen_ids:
            continue
        seen_ids.add(screen.id)
        screens.append(screen)
    return screens


def validate_configured_screens(raw_items: object) -> list[ConfiguredScreen]:
    if not isinstance(raw_items, list):
        raise ValueError("Configured screens payload must be a list.")
    screens: list[ConfiguredScreen] = []
    seen_ids: set[str] = set()
    for item in raw_items:
        if not isinstance(item, Mapping):
            raise ValueError("Each configured screen must be an object.")
        screen = ConfiguredScreen.from_dict(item)
        if screen.id in seen_ids:
            raise ValueError("Configured screen ids must be unique.")
        seen_ids.add(screen.id)
        screens.append(screen)
    return screens
