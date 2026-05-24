from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from browser_screen_bindings import (
    browser_target_status_detail,
    configured_browser_screen_ids,
)
from models import ScreenRecord, UnifiedScreenTarget
from screen_groups import ScreenGroup, combined_screen_groups
from screen_protocols import ConfiguredScreen


@dataclass(slots=True)
class ScreenRegistry:
    owner: Any

    def configured_screens(self) -> list[ConfiguredScreen]:
        screens = getattr(self.owner, "configured_screens", [])
        return list(screens) if isinstance(screens, list) else []

    def screen_groups(self) -> list[ScreenGroup]:
        groups = getattr(self.owner, "screen_groups", [])
        return list(groups) if isinstance(groups, list) else []

    def selected_monitor_ids(self) -> list[str]:
        selected = getattr(self.owner, "selected_monitor_ids", [])
        return [str(item) for item in selected] if isinstance(selected, list) else []

    def enabled_screen_ids(self) -> list[str]:
        enabled = getattr(self.owner, "enabled_screen_ids", [])
        return [str(item) for item in enabled] if isinstance(enabled, list) else []

    def screen_aliases(self) -> dict[str, str]:
        aliases = getattr(self.owner, "screen_aliases", {})
        if not isinstance(aliases, dict):
            return {}
        return {str(key): str(value) for key, value in aliases.items()}

    def remote_screens(self) -> dict[str, dict[str, Any]]:
        remote = getattr(self.owner, "remote_screens", {})
        if not isinstance(remote, dict):
            return {}
        normalized: dict[str, dict[str, Any]] = {}
        for key, value in remote.items():
            if not isinstance(value, dict):
                continue
            normalized[str(key)] = value
        return normalized

    def all_screen_groups(self) -> list[ScreenGroup]:
        return combined_screen_groups(self.selected_monitor_ids(), self.screen_groups())

    def configured_screen_name_map(self) -> dict[str, str]:
        return {
            screen.id: screen.name
            for screen in self.configured_screens()
            if screen.name.strip()
        }

    def browser_configured_screens(self) -> list[ConfiguredScreen]:
        return [
            screen
            for screen in self.configured_screens()
            if screen.transport == "browser"
        ]

    def browser_configured_screen_ids(self) -> set[str]:
        return configured_browser_screen_ids(self.configured_screens())

    def static_media_configured_screen_ids(self) -> set[str]:
        return {
            screen.id
            for screen in self.configured_screens()
            if "static_media" in screen.capabilities
        }

    def unified_screen_records(
        self,
        *,
        local_screens: list[Any],
        screen_identifier: Callable[[Any], str],
        screen_display_name: Callable[[Any, dict[str, str]], str],
        include_offline: bool = True,
    ) -> list[ScreenRecord]:
        records: list[ScreenRecord] = []
        aliases = self.screen_aliases()
        enabled_ids = set(self.enabled_screen_ids())
        static_media_ids = self.static_media_configured_screen_ids()
        remote_screens = self.remote_screens()

        for local_screen in local_screens:
            identifier = screen_identifier(local_screen)
            geometry = local_screen.geometry()
            records.append(
                ScreenRecord(
                    id=identifier,
                    display_name=screen_display_name(local_screen, aliases),
                    transport="local_hdmi",
                    online=True,
                    enabled=(not enabled_ids or identifier in enabled_ids),
                    source="detected",
                    metadata={
                        "detail": (
                            f"HDMI / local display • {geometry.width()}x{geometry.height()}"
                        )
                    },
                )
            )

        for configured in sorted(
            self.browser_configured_screens(), key=lambda item: item.name.lower()
        ):
            if configured.id not in static_media_ids:
                continue
            online, detail, warning = browser_target_status_detail(
                configured, remote_screens
            )
            if not include_offline and not online:
                continue
            records.append(
                ScreenRecord(
                    id=configured.id,
                    display_name=configured.name,
                    transport="browser",
                    online=online,
                    enabled=(not enabled_ids or configured.id in enabled_ids),
                    source="configured",
                    metadata={"detail": detail, "warning": warning},
                )
            )

        return records

    def unified_screen_targets(
        self,
        *,
        local_screens: list[Any],
        screen_identifier: Callable[[Any], str],
        screen_display_name: Callable[[Any, dict[str, str]], str],
        include_offline: bool = True,
    ) -> list[UnifiedScreenTarget]:
        records = self.unified_screen_records(
            local_screens=local_screens,
            screen_identifier=screen_identifier,
            screen_display_name=screen_display_name,
            include_offline=include_offline,
        )
        targets: list[UnifiedScreenTarget] = []
        for record in records:
            targets.append(
                UnifiedScreenTarget(
                    id=record.id,
                    label=record.display_name,
                    kind="remote" if record.transport == "browser" else "local",
                    online=record.online,
                    detail=str(record.metadata.get("detail") or ""),
                    warning=str(record.metadata.get("warning") or ""),
                )
            )
        return targets
