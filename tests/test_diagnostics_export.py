from __future__ import annotations

import json
import zipfile
from pathlib import Path

import main as main_module


def _patch_paths(monkeypatch, base_dir: Path) -> None:
    app_data_dir = base_dir / "app_data"
    startup_status = app_data_dir / "engine_startup_status.json"
    startup_log = app_data_dir / "engine_startup.log"
    diagnostics_log = app_data_dir / "engine_diagnostics.jsonl"
    monkeypatch.setattr(main_module, "APP_DATA_DIR", app_data_dir)
    monkeypatch.setattr(main_module, "LEGACY_APP_DATA_DIR", app_data_dir)
    monkeypatch.setattr(main_module, "ENGINE_STARTUP_STATUS_PATH", startup_status)
    monkeypatch.setattr(main_module, "ENGINE_STARTUP_LOG_PATH", startup_log)
    monkeypatch.setattr(main_module, "ENGINE_DIAGNOSTICS_LOG_PATH", diagnostics_log)


def test_export_engine_diagnostics_bundle_writes_zip_with_snapshot_and_logs(
    monkeypatch, tmp_path
) -> None:
    _patch_paths(monkeypatch, tmp_path)
    main_module.ensure_app_paths()

    main_module.ENGINE_STARTUP_STATUS_PATH.write_text(
        json.dumps({"state": "ready"}), encoding="utf-8"
    )
    main_module.ENGINE_STARTUP_LOG_PATH.write_text("startup ok\n", encoding="utf-8")
    main_module.append_engine_diagnostics_event("engine_action_succeeded")

    destination = tmp_path / "exports" / "diag.zip"
    snapshot = {"stateVersion": 3, "channels": ["runtime"]}
    output_path = main_module.export_engine_diagnostics_bundle(
        destination=destination,
        snapshot=snapshot,
    )

    assert output_path == destination
    assert destination.exists()

    with zipfile.ZipFile(destination, "r") as archive:
        names = set(archive.namelist())
        assert "metadata.json" in names
        assert "snapshot.json" in names
        assert "engine_startup_status.json" in names
        assert "engine_startup.log" in names
        assert "engine_diagnostics.jsonl" in names

        parsed_snapshot = json.loads(archive.read("snapshot.json").decode("utf-8"))
        assert parsed_snapshot == snapshot
