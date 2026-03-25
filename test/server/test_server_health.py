import pytest
import json
import asyncio
from unittest.mock import patch, MagicMock
from fastapi import FastAPI
from main import lifespan


@pytest.mark.asyncio
async def test_heartbeat_emitter_schema():
    """
    Tests that the server's background heartbeat task correctly
    formats its metrics payload and broadcasts over UDP.
    Isolated from DnD rules/mechanics tests.
    """
    app = FastAPI()

    # Mock socket to intercept the UDP payload, and subprocess to prevent spawning QA agents during tests
    with patch("socket.socket") as mock_socket_class, patch("subprocess.Popen"):
        mock_sock = MagicMock()
        mock_socket_class.return_value = mock_sock

        # Start the lifespan context manager
        async with lifespan(app):
            # Yield control to the event loop so the heartbeat task runs at least once
            await asyncio.sleep(1.2)

        # Verify heartbeats were dispatched (local monitoring + LAN broadcast)
        assert mock_sock.sendto.called, "Heartbeat was not emitted."

        # Collect all calls
        calls = mock_sock.sendto.call_args_list

        # Find the local monitoring heartbeat (127.0.0.1:9999)
        local_call = None
        broadcast_call = None
        for call in calls:
            args = call[0]
            if args[1] == ("127.0.0.1", 9999):
                local_call = args
            elif args[1] == ("255.255.255.255", 9998):
                broadcast_call = args

        assert local_call is not None, "Local heartbeat (127.0.0.1:9999) not found"

        # Validate local monitoring payload
        payload = local_call[0]
        data = json.loads(payload.decode("utf-8"))

        assert "pid" in data and isinstance(data["pid"], int)
        assert "cpu_percent" in data and isinstance(data["cpu_percent"], (float, int))
        assert "mem_mb" in data and isinstance(data["mem_mb"], (float, int))
        assert "timestamp" in data and isinstance(data["timestamp"], float)

        # Validate LAN broadcast payload (if present)
        if broadcast_call is not None:
            bcast_payload = broadcast_call[0]
            bcast_data = json.loads(bcast_payload.decode("utf-8"))
            assert "server_name" in bcast_data
            assert "campaign" in bcast_data
            assert "port" in bcast_data