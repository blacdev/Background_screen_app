from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import main as main_module
from main import ControllerEngine


def test_controller_api_wait_for_local_worker_command_builds_query(monkeypatch) -> None:
    client = main_module.ControllerApiClient(port=1234)
    captured: dict[str, object] = {}

    def _fake_request(method: str, path: str, payload=None, timeout: float = 0.0):
        captured["method"] = method
        captured["path"] = path
        captured["timeout"] = timeout
        return {"ok": True}

    monkeypatch.setattr(client, "_request", _fake_request)
    client.wait_for_local_worker_command("display-1", 22, timeout=2.5)

    assert captured["method"] == "GET"
    assert "screen_id=display-1" in str(captured["path"])
    assert "since=22" in str(captured["path"])
    assert "timeout_ms=2500" in str(captured["path"])


def test_wait_for_local_worker_command_returns_immediate_update() -> None:
    command = {"version": 8, "type": "play"}
    engine = SimpleNamespace(
        run_on_engine_thread=lambda fn: fn(),
        local_worker_command_payload=lambda screen_id: command,
        wait_for_state_update=lambda since, timeout: (False, since),
    )

    result = ControllerEngine.wait_for_local_worker_command(
        cast(Any, engine),
        "display-1",
        since_version=3,
        timeout=0.1,
    )

    assert result["updated"] is True
    assert result["commandVersion"] == 8
    assert result["command"] == command


def test_wait_for_local_worker_command_times_out_when_unchanged() -> None:
    command = {"version": 5, "type": "clear"}
    wait_calls: list[tuple[int, float]] = []

    def _wait_for_state_update(since: int, timeout: float) -> tuple[bool, int]:
        wait_calls.append((since, timeout))
        return False, since

    engine = SimpleNamespace(
        run_on_engine_thread=lambda fn: fn(),
        local_worker_command_payload=lambda screen_id: command,
        wait_for_state_update=_wait_for_state_update,
    )

    result = ControllerEngine.wait_for_local_worker_command(
        cast(Any, engine),
        "display-1",
        since_version=5,
        timeout=0.0,
    )

    assert result["updated"] is False
    assert result["commandVersion"] == 5
    assert result["command"] == command
    assert wait_calls == []
