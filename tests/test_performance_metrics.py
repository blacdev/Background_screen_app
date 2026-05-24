from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import main as main_module


def test_performance_metrics_payload_includes_scan_snapshot_and_worker_counters() -> (
    None
):
    engine_like = SimpleNamespace(
        last_media_scan_duration_ms=321,
        last_media_scan_file_count=42,
        last_snapshot_size_bytes=8192,
        local_playback_workers={"screen-a": object(), "screen-b": object()},
        last_snapshot_generated_at="2026-05-24T12:00:00",
    )

    payload = main_module.ControllerEngine.performance_metrics_payload(
        engine_like  # type: ignore[arg-type]
    )

    assert payload == {
        "mediaScanDurationMs": 321,
        "indexedFileCount": 42,
        "snapshotSizeBytes": 8192,
        "localPlaybackWorkerCount": 2,
        "lastSnapshotGeneratedAt": "2026-05-24T12:00:00",
    }


def test_performance_metrics_payload_normalizes_numeric_fields() -> None:
    engine_like: Any = SimpleNamespace(
        last_media_scan_duration_ms="15",
        last_media_scan_file_count="3",
        last_snapshot_size_bytes="1024",
        local_playback_workers={},
        last_snapshot_generated_at="",
    )

    payload = main_module.ControllerEngine.performance_metrics_payload(engine_like)

    assert payload["mediaScanDurationMs"] == 15
    assert payload["indexedFileCount"] == 3
    assert payload["snapshotSizeBytes"] == 1024
    assert payload["localPlaybackWorkerCount"] == 0
