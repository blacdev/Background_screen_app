from __future__ import annotations

import main as main_module


def test_unpaired_client_is_added_to_pending_and_blocked_when_pairing_required() -> (
    None
):
    server = main_module.LanRemoteServer(port=0)
    server.configure_pairing(
        required=True, allow_unpaired_clients=False, paired_client_ids=[]
    )

    register = server.register_or_refresh(
        {
            "name": "Lobby TV",
            "clientId": "client-lobby",
            "userAgent": "pytest-agent",
            "width": 1920,
            "height": 1080,
        },
        "10.0.0.10",
    )
    screen_id = str(register.get("screenId") or "")

    server.set_commands(
        {
            screen_id: {
                "screenId": screen_id,
                "version": 7,
                "type": "play",
                "path": "demo.mp4",
                "transition": "fade_black",
            }
        }
    )

    snapshot = server.network_snapshot()
    command = server.command_for_screen(screen_id)

    assert snapshot.get("pairingRequired") is True
    assert int(snapshot.get("pendingClientCount") or 0) == 1
    pending = snapshot.get("pendingClients") or []
    assert isinstance(pending, list)
    assert pending and pending[0].get("client_id") == "client-lobby"
    assert command.get("type") == "clear"
    assert "Pair this screen" in str(command.get("message") or "")


def test_unpaired_client_can_receive_commands_when_allow_unpaired_enabled() -> None:
    server = main_module.LanRemoteServer(port=0)
    server.configure_pairing(
        required=True, allow_unpaired_clients=True, paired_client_ids=[]
    )

    register = server.register_or_refresh(
        {
            "name": "Reception TV",
            "clientId": "client-reception",
            "userAgent": "pytest-agent",
        },
        "10.0.0.11",
    )
    screen_id = str(register.get("screenId") or "")

    server.set_commands(
        {
            screen_id: {
                "screenId": screen_id,
                "version": 11,
                "type": "play",
                "path": "demo.mp4",
                "transition": "fade_black",
            }
        }
    )

    command = server.command_for_screen(screen_id)

    assert command.get("type") == "play"
    assert int(command.get("version") or 0) == 11
