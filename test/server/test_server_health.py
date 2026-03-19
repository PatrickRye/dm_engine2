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
            
        # Verify the heartbeat was dispatched
        assert mock_sock.sendto.called, "Heartbeat was not emitted."
        
        # Extract arguments from the last sendto call
        args, _ = mock_sock.sendto.call_args
        payload = args[0]
        target_addr = args[1]
        
        assert target_addr == ("127.0.0.1", 9999)
        
        # Parse and validate the JSON schema
        data = json.loads(payload.decode("utf-8"))
        
        assert "pid" in data and isinstance(data["pid"], int)
        assert "cpu_percent" in data and isinstance(data["cpu_percent"], (float, int))
        assert "mem_mb" in data and isinstance(data["mem_mb"], (float, int))
        assert "timestamp" in data and isinstance(data["timestamp"], float)