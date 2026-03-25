"""Tests for visibility endpoint and state change notifications."""

import pytest
import asyncio
import time
from unittest.mock import patch, MagicMock

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../..", "server"))

from main import (
    lifespan,
    push_state_change,
    get_and_clear_state_changes,
    VisibilityRequest,
    STATE_CHANGES,
    STATE_CHANGES_LOCK,
)
from fastapi import FastAPI


@pytest.fixture(autouse=True)
def clear_state():
    """Clear state changes before and after each test."""
    async def _clear():
        async with STATE_CHANGES_LOCK:
            STATE_CHANGES.clear()
    # Clear before
    asyncio.run(_clear())
    yield
    # Clear after
    asyncio.run(_clear())


class TestStateChangeNotifications:
    """Tests for the push_state_change and get_and_clear_state_changes functions."""

    @pytest.mark.asyncio
    async def test_push_state_change_adds_to_queue(self):
        """Test that push_state_change adds a change to the queue."""
        push_state_change("hp_change", "Thorin", {"hp": 30, "max_hp": 45})
        await asyncio.sleep(0.01)

        changes = await get_and_clear_state_changes()
        assert len(changes) == 1
        assert changes[0]["type"] == "hp_change"
        assert changes[0]["entity"] == "Thorin"
        assert changes[0]["changes"]["hp"] == 30

    @pytest.mark.asyncio
    async def test_get_and_clear_state_changes_clears_queue(self):
        """Test that get_and_clear_state_changes clears the queue after returning."""
        push_state_change("condition", "Elminster", {"conditions": ["Poisoned"]})
        await asyncio.sleep(0.01)

        changes = await get_and_clear_state_changes()
        assert len(changes) == 1

        # Second call should return empty
        changes2 = await get_and_clear_state_changes()
        assert len(changes2) == 0

    @pytest.mark.asyncio
    async def test_push_state_change_keeps_only_recent(self):
        """Test that only the last 100 state changes are kept."""
        for i in range(105):
            push_state_change("hp_change", f"Char{i}", {"hp": i})
        await asyncio.sleep(0.01)

        changes = await get_and_clear_state_changes()
        # Should only have 100 (the last 100)
        assert len(changes) == 100
        # First one should be Char5 (index 5), not Char0
        assert changes[0]["entity"] == "Char5"
        # Last one should be Char104
        assert changes[-1]["entity"] == "Char104"

    @pytest.mark.asyncio
    async def test_state_change_contains_timestamp(self):
        """Test that state changes include a timestamp."""
        before = time.time()
        push_state_change("hp_change", "Test", {"hp": 10})
        after = time.time()
        await asyncio.sleep(0.01)

        changes = await get_and_clear_state_changes()

        assert changes[0]["timestamp"] >= before
        assert changes[0]["timestamp"] <= after


class TestVisibilityEndpoint:
    """Tests for the /visibility endpoint."""

    @pytest.mark.asyncio
    async def test_visibility_endpoint_no_vault(self):
        """Test visibility returns no_vault when no vault is active."""
        with patch("socket.socket") as mock_socket_class, patch("subprocess.Popen"):
            mock_sock = MagicMock()
            mock_socket_class.return_value = mock_sock

            async with lifespan(FastAPI()):
                from main import visibility_endpoint

                request = VisibilityRequest(
                    character="Thorin",
                    target_x=10.0,
                    target_y=20.0,
                    vault_path=""
                )

                result = await visibility_endpoint(request)
                assert result["visible"] is False
                assert result["reason"] == "no_vault"


class TestHeartbeatStateChanges:
    """Tests that heartbeat returns state changes."""

    @pytest.mark.asyncio
    async def test_heartbeat_returns_state_changes(self):
        """Test that heartbeat includes state_changes in response."""
        with patch("socket.socket") as mock_socket_class, patch("subprocess.Popen"):
            mock_sock = MagicMock()
            mock_socket_class.return_value = mock_sock

            async with lifespan(FastAPI()):
                from main import heartbeat_endpoint, HeartbeatRequest

                # Push some state changes
                push_state_change("hp_change", "Thorin", {"hp": 25, "max_hp": 45})

                request = HeartbeatRequest(
                    client_id="test-client",
                    character="Human DM",
                    roll_automations={}
                )

                result = await heartbeat_endpoint(request)

                # State changes should be in the response
                assert "state_changes" in result
                assert len(result["state_changes"]) == 1
                assert result["state_changes"][0]["entity"] == "Thorin"


class TestCharacterSheetMerging:
    """Tests for character sheet endpoint merging engine state with YAML."""

    @pytest.mark.asyncio
    async def test_character_sheet_returns_human_dm(self):
        """Test that Human DM returns proper placeholder data."""
        with patch("socket.socket") as mock_socket_class, patch("subprocess.Popen"):
            mock_sock = MagicMock()
            mock_socket_class.return_value = mock_sock

            async with lifespan(FastAPI()):
                from main import character_sheet_endpoint, CharSheetRequest

                request = CharSheetRequest(
                    character="Human DM",
                    vault_path="test_vault"
                )

                result = await character_sheet_endpoint(request)

                assert "sheet" in result
                sheet = result["sheet"]
                assert sheet["name"] == "Human DM"
                assert sheet["role"] == "Dungeon Master"
                assert sheet["hp"] == "—"  # Not infinity, proper em-dash
                assert sheet["ac"] == "—"
                assert sheet["conditions"] == []
