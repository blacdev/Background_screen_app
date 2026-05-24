from __future__ import annotations

import json
from pathlib import Path

import main as main_module


def _patch_paths(monkeypatch, base_dir: Path) -> Path:
    app_data_dir = base_dir / "app_data"
    startup_status = app_data_dir / "engine_startup_status.json"
    startup_log = app_data_dir / "engine_startup.log"
    diagnostics_log = app_data_dir / "engine_diagnostics.jsonl"
    monkeypatch.setattr(main_module, "APP_DATA_DIR", app_data_dir)
    monkeypatch.setattr(main_module, "LEGACY_APP_DATA_DIR", app_data_dir)
    monkeypatch.setattr(main_module, "ENGINE_STARTUP_STATUS_PATH", startup_status)
    monkeypatch.setattr(main_module, "ENGINE_STARTUP_LOG_PATH", startup_log)
    monkeypatch.setattr(main_module, "ENGINE_DIAGNOSTICS_LOG_PATH", diagnostics_log)
    return diagnostics_log


def test_append_engine_diagnostics_event_writes_jsonl(monkeypatch, tmp_path) -> None:
    diagnostics_log = _patch_paths(monkeypatch, tmp_path)

    main_module.append_engine_diagnostics_event(
        "engine_action_succeeded",
        details="Action quick_play applied.",
        context={"action": "quick_play"},
    )

    assert diagnostics_log.exists()
    lines = diagnostics_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "engine_action_succeeded"
    assert payload["severity"] == "info"
    assert payload["context"]["action"] == "quick_play"


def test_reset_engine_startup_artifacts_removes_diagnostics_log(
    monkeypatch, tmp_path
) -> None:
    diagnostics_log = _patch_paths(monkeypatch, tmp_path)

    main_module.append_engine_diagnostics_event("test_event")
    assert diagnostics_log.exists()

    main_module.reset_engine_startup_artifacts()

    assert not diagnostics_log.exists()
