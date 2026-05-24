from __future__ import annotations

from types import SimpleNamespace

import main as main_module
from models import UnifiedScreenTarget


class _FakeComboBox:
    def __init__(self) -> None:
        self._items: list[tuple[str, str]] = []
        self._enabled = True

    def blockSignals(self, _blocked: bool) -> None:
        return

    def clear(self) -> None:
        self._items = []

    def addItem(self, label: str, value: str) -> None:
        self._items.append((label, value))

    def currentData(self):
        if not self._items:
            return ""
        return self._items[0][1]

    def setEnabled(self, enabled: bool) -> None:
        self._enabled = enabled


class _FakeLabel:
    def __init__(self) -> None:
        self.text = ""

    def setText(self, text: str) -> None:
        self.text = text


class _FakeButton:
    def __init__(self) -> None:
        self.enabled = True

    def setEnabled(self, enabled: bool) -> None:
        self.enabled = enabled


def _target(screen_id: str, kind: str = "remote") -> UnifiedScreenTarget:
    return UnifiedScreenTarget(
        id=screen_id,
        label=screen_id,
        kind=kind,
        online=True,
        detail="",
        warning="",
    )


def test_refresh_merge_controls_populates_source_target_and_enables_action() -> None:
    dialog = SimpleNamespace(
        all_targets=[
            _target("remote:one", "remote"),
            _target("remote:two", "remote"),
            _target("local:one", "local"),
        ],
        screen_aliases={"remote:one": "Lobby", "remote:two": "Reception"},
        merge_handler=lambda source, target: (True, f"{source}->{target}"),
        merge_source_selector=_FakeComboBox(),
        merge_target_selector=_FakeComboBox(),
        merge_apply_button=_FakeButton(),
        merge_feedback_label=_FakeLabel(),
    )

    main_module.ManageScreensWorkspaceDialog._merge_candidate_ids(dialog)
    dialog._merge_candidate_ids = lambda: (
        main_module.ManageScreensWorkspaceDialog._merge_candidate_ids(dialog)
    )
    dialog._sync_merge_targets = lambda: (
        main_module.ManageScreensWorkspaceDialog._sync_merge_targets(dialog)
    )

    main_module.ManageScreensWorkspaceDialog._refresh_merge_controls(dialog)

    source_values = [value for _label, value in dialog.merge_source_selector._items]
    target_values = [value for _label, value in dialog.merge_target_selector._items]

    assert source_values == ["remote:one", "remote:two"]
    assert target_values == ["remote:two"]
    assert dialog.merge_apply_button.enabled is True


def test_apply_merge_from_workspace_invokes_handler_and_updates_local_state() -> None:
    captured: list[tuple[str, str]] = []

    def _merge_handler(source: str, target: str) -> tuple[bool, str]:
        captured.append((source, target))
        return True, "Merged duplicate remembered screen into target."

    source_selector = _FakeComboBox()
    source_selector.addItem("Source", "remote:dup")
    target_selector = _FakeComboBox()
    target_selector.addItem("Target", "remote:keep")

    populate_calls: list[bool] = []
    refresh_calls: list[bool] = []

    dialog = SimpleNamespace(
        merge_source_selector=source_selector,
        merge_target_selector=target_selector,
        merge_feedback_label=_FakeLabel(),
        merge_handler=_merge_handler,
        selected_monitor_ids={"remote:dup", "local:one"},
        enabled_screen_ids={"remote:dup"},
        screen_aliases={"remote:dup": "Dup", "remote:keep": "Keep"},
        all_targets=[_target("remote:dup"), _target("remote:keep")],
        populate_list=lambda: populate_calls.append(True),
        _refresh_merge_controls=lambda: refresh_calls.append(True),
    )

    main_module.ManageScreensWorkspaceDialog._apply_merge_from_workspace(dialog)

    assert captured == [("remote:dup", "remote:keep")]
    assert "remote:dup" not in dialog.selected_monitor_ids
    assert "remote:dup" not in dialog.enabled_screen_ids
    assert "remote:dup" not in dialog.screen_aliases
    assert [target.id for target in dialog.all_targets] == ["remote:keep"]
    assert (
        dialog.merge_feedback_label.text
        == "Merged duplicate remembered screen into target."
    )
    assert populate_calls == [True]
    assert refresh_calls == [True]
