from __future__ import annotations

import main as main_module


def test_lan_remote_server_set_commands_isolated_from_input_mutation() -> None:
    server = main_module.LanRemoteServer(port=9998)
    commands = {
        "remote:one": {
            "protocolVersion": 2,
            "screenId": "remote:one",
            "version": 1,
            "type": "play",
            "mode": "schedule",
            "path": "C:/media/a.jpg",
            "paths": ["C:/media/a.jpg"],
            "mediaUrl": "/media/a.jpg",
            "mediaUrls": ["/media/a.jpg"],
            "playAtMs": 123,
        }
    }

    server.set_commands(commands)
    commands["remote:one"]["version"] = 999
    commands["remote:one"]["paths"].append("C:/media/other.jpg")

    payload = server.command_for_screen("remote:one")
    assert payload["version"] == 1
    assert payload["paths"] == ["C:/media/a.jpg"]


def test_lan_remote_server_command_for_screen_returns_isolated_payload_copy() -> None:
    server = main_module.LanRemoteServer(port=9997)
    server.set_commands(
        {
            "remote:one": {
                "protocolVersion": 2,
                "screenId": "remote:one",
                "version": 2,
                "type": "play",
                "mode": "schedule",
                "path": "C:/media/a.jpg",
                "paths": ["C:/media/a.jpg"],
                "mediaUrl": "/media/a.jpg",
                "mediaUrls": ["/media/a.jpg"],
                "playAtMs": 123,
            }
        }
    )

    first = server.command_for_screen("remote:one")
    first["version"] = 100
    first["paths"].append("C:/media/b.jpg")

    second = server.command_for_screen("remote:one")
    assert second["version"] == 2
    assert second["paths"] == ["C:/media/a.jpg"]
