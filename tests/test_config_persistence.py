from __future__ import annotations

from pathlib import Path

import main as main_module


def _patch_config_paths(monkeypatch, base_dir: Path) -> Path:
    app_data_dir = base_dir / "app_data"
    config_path = app_data_dir / "config.json"
    monkeypatch.setattr(main_module, "APP_DATA_DIR", app_data_dir)
    monkeypatch.setattr(main_module, "LEGACY_APP_DATA_DIR", app_data_dir)
    monkeypatch.setattr(main_module, "CONFIG_PATH", config_path)
    return config_path


def test_load_config_defaults_include_schema_version(monkeypatch, tmp_path) -> None:
    config_path = _patch_config_paths(monkeypatch, tmp_path)

    payload = main_module.load_config()

    assert payload["schema_version"] == main_module.CONFIG_SCHEMA_VERSION
    assert payload["configured_screens"] == []
    assert payload["screen_groups"] == []
    assert payload["schedules"] == []
    assert not config_path.exists()


def test_load_config_uses_current_schema_version_for_legacy_payloads(
    monkeypatch, tmp_path
) -> None:
    config_path = _patch_config_paths(monkeypatch, tmp_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        """
        {
          "configured_screens": [],
          "screen_groups": [],
          "selected_monitor_ids": ["monitor-1"],
          "enabled_screen_ids": ["screen-1"],
          "video_directory": "videos",
          "screen_aliases": {},
          "transition_method": "cut",
          "run_at_startup": true,
          "schedules": []
        }
        """.strip(),
        encoding="utf-8",
    )

    payload = main_module.load_config()

    assert payload["schema_version"] == main_module.CONFIG_SCHEMA_VERSION
    assert payload["selected_monitor_ids"] == ["monitor-1"]
    assert payload["enabled_screen_ids"] == ["screen-1"]
    assert payload["transition_method"] == "cut"
    assert payload["run_at_startup"] is True


def test_migrate_config_adds_schema_version_and_default_keys() -> None:
    payload = main_module.migrate_config(
        {
            "selected_monitor_ids": ["monitor-1"],
            "video_directory": "videos",
        }
    )

    assert payload["schema_version"] == main_module.CONFIG_SCHEMA_VERSION
    assert payload["selected_monitor_ids"] == ["monitor-1"]
    assert payload["video_directory"] == "videos"
    assert payload["configured_screens"] == []
    assert payload["screen_groups"] == []
    assert payload["enabled_screen_ids"] == []
    assert payload["schedules"] == []
    assert payload["screen_registry"] == []


def test_migrate_config_v1_to_v2_adds_screen_registry_snapshot() -> None:
    payload = main_module.migrate_config(
        {
            "schema_version": 1,
            "configured_screens": [{"id": "screen-1"}],
            "selected_monitor_ids": [],
            "enabled_screen_ids": [],
            "video_directory": "",
            "screen_aliases": {},
            "transition_method": "fade_black",
            "run_at_startup": False,
            "screen_groups": [],
            "schedules": [],
        }
    )

    assert payload["schema_version"] == main_module.CONFIG_SCHEMA_VERSION
    assert payload["screen_registry"] == [{"id": "screen-1"}]


def test_migrate_config_preserves_future_schema_versions() -> None:
    payload = main_module.migrate_config(
        {
            "schema_version": 99,
            "custom": "future",
        }
    )

    assert payload["schema_version"] == 99
    assert payload["custom"] == "future"
    assert "configured_screens" in payload


def test_migrate_config_returns_defaults_for_invalid_payloads() -> None:
    payload = main_module.migrate_config("not-a-dict")

    assert payload == main_module._default_config_payload()


def test_save_config_writes_schema_version_and_round_trips(
    monkeypatch, tmp_path
) -> None:
    config_path = _patch_config_paths(monkeypatch, tmp_path)

    main_module.save_config(
        configured_screens=[],
        screen_groups=[],
        selected_monitor_ids=["monitor-1"],
        enabled_screen_ids=["screen-1"],
        video_directory="videos",
        screen_aliases={"screen-1": "Lobby"},
        transition_method="fade_black",
        run_at_startup=True,
        schedules=[],
    )

    assert config_path.exists()
    payload = main_module.load_config()
    assert payload["schema_version"] == main_module.CONFIG_SCHEMA_VERSION
    assert payload["selected_monitor_ids"] == ["monitor-1"]
    assert payload["enabled_screen_ids"] == ["screen-1"]
    assert payload["screen_aliases"] == {"screen-1": "Lobby"}
    assert payload["run_at_startup"] is True
