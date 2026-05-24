from __future__ import annotations

import main as main_module


def test_format_engine_diagnostics_summary_for_normal_state() -> None:
    primary, secondary, tooltip = main_module.format_engine_diagnostics_summary(
        {
            "indexedFileCount": 42,
            "mediaScanDurationMs": 315,
            "snapshotSizeBytes": 8192,
            "localPlaybackWorkerCount": 2,
            "lastSnapshotGeneratedAt": "2026-05-24T12:00:00",
        }
    )

    assert primary == "2 workers"
    assert secondary == "Library 42 file(s) • last scan 315 ms • snapshot 8192 bytes"
    assert "Indexed media files: 42" in tooltip
    assert "Local playback workers: 2" in tooltip
    assert "Last snapshot generated: 2026-05-24T12:00:00" in tooltip


def test_format_engine_diagnostics_summary_for_scanning_state() -> None:
    primary, secondary, tooltip = main_module.format_engine_diagnostics_summary(
        {
            "indexedFileCount": 7,
            "mediaScanDurationMs": 0,
            "snapshotSizeBytes": 1024,
            "localPlaybackWorkerCount": 1,
        },
        scanning=True,
    )

    assert primary == "1 worker"
    assert secondary == "Library indexing • 7 file(s) found so far"
    assert "Library state: indexing in progress" in tooltip


def test_format_engine_diagnostics_summary_for_library_error() -> None:
    primary, secondary, tooltip = main_module.format_engine_diagnostics_summary(
        {
            "indexedFileCount": 0,
            "mediaScanDurationMs": 22,
            "snapshotSizeBytes": 256,
            "localPlaybackWorkerCount": 0,
        },
        library_error="Access denied",
    )

    assert primary == "0 workers"
    assert secondary == "Library error • last scan 22 ms"
    assert "Library error: Access denied" in tooltip
