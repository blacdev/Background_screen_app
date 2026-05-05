from __future__ import annotations

from typing import Any

from screen_protocols import ConfiguredScreen


BROWSER_BINDING_KEY = "remote_screen_id"
LEGACY_BINDING_KEY = "value"


def browser_binding_remote_id(screen: ConfiguredScreen) -> str:
    if screen.transport != "browser":
        return ""
    binding = screen.binding or {}
    explicit = str(binding.get(BROWSER_BINDING_KEY) or "").strip()
    if explicit.startswith("remote:"):
        return explicit
    legacy = str(binding.get(LEGACY_BINDING_KEY) or "").strip()
    if legacy.startswith("remote:"):
        return legacy
    return ""


def configured_browser_screen_ids(configured_screens: list[ConfiguredScreen]) -> set[str]:
    return {screen.id for screen in configured_screens if screen.transport == "browser"}


def validate_unique_browser_bindings(configured_screens: list[ConfiguredScreen]) -> None:
    seen: dict[str, str] = {}
    for screen in configured_screens:
        remote_id = browser_binding_remote_id(screen)
        if not remote_id:
            continue
        previous = seen.get(remote_id)
        if previous is not None:
            raise ValueError("Each connected browser receiver can only be bound to one configured browser screen.")
        seen[remote_id] = screen.id


def bound_remote_state(screen: ConfiguredScreen, remote_screens: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    remote_id = browser_binding_remote_id(screen)
    if not remote_id:
        return None
    state = remote_screens.get(remote_id)
    return dict(state) if isinstance(state, dict) else None


def browser_target_status_detail(screen: ConfiguredScreen, remote_screens: dict[str, dict[str, Any]]) -> tuple[bool, str, str]:
    remote_id = browser_binding_remote_id(screen)
    if not remote_id:
        return False, "Browser receiver • Unbound", "Choose a connected browser receiver for this screen."
    state = bound_remote_state(screen, remote_screens)
    if state is None:
        return False, f"Browser receiver • Bound to {remote_id}", "The bound browser receiver is not currently connected."
    online = bool(state.get("online"))
    size_text = ""
    width = int(state.get("width") or 0)
    height = int(state.get("height") or 0)
    if width and height:
        size_text = f" • {width}x{height}"
    detail = f"Browser receiver • {'Online' if online else 'Offline'}"
    ip = str(state.get("ip") or "").strip()
    if ip:
        detail += f" • {ip}"
    detail += size_text
    warning = "" if online else "The bound browser receiver is offline."
    return online, detail, warning


def resolve_browser_commands(
    configured_screens: list[ConfiguredScreen],
    remote_screens: dict[str, dict[str, Any]],
    screen_commands: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    resolved: dict[str, dict[str, Any]] = {}
    screen_by_id = {screen.id: screen for screen in configured_screens if screen.transport == "browser"}
    for screen_id, command in screen_commands.items():
        screen = screen_by_id.get(screen_id)
        if screen is None:
            continue
        remote_id = browser_binding_remote_id(screen)
        if not remote_id or remote_id not in remote_screens:
            continue
        translated = dict(command)
        translated["screenId"] = remote_id
        translated["configuredScreenId"] = screen.id
        translated["configuredScreenName"] = screen.name
        resolved[remote_id] = translated
    return resolved
