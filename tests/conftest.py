import os
import shutil
import tempfile
import pytest
from unittest.mock import patch
from dnd_rules_engine import EventBus
from event_handlers import register_core_handlers

@pytest.fixture(autouse=True)
def mock_obsidian_vault():
    """
    Global fixture that runs before EVERY test.
    It creates a fresh, isolated temporary directory and copies
    everything from tests/resources/vault into it.
    It then patches the app to use this temp directory instead of the real vault.
    """
    base_dir = os.path.dirname(__file__)
    resource_vault = os.path.join(base_dir, "resources", "vault")
    
    # Ensure the EventBus is fully populated before every test to prevent cross-test contamination
    register_core_handlers()
    
    with tempfile.TemporaryDirectory() as temp_dir:
        # Setup isolated vault space
        journals_dir = os.path.join(temp_dir, "Journals")
        os.makedirs(journals_dir, exist_ok=True)
        
        # If you drop "Curse_of_Strahd.md" or other files into tests/resources/vault, 
        # they will be cleanly copied into this ephemeral test vault.
        if os.path.exists(resource_vault):
            shutil.copytree(resource_vault, temp_dir, dirs_exist_ok=True)
            
        # Globally patch any vault resolving functions to point to our temp_dir
        # This intercepts the local file interactions!
        patcher1 = patch("vault_io.get_journals_dir", return_value=journals_dir)
        patcher2 = patch("tools.get_journals_dir", return_value=journals_dir)
        patcher3 = patch("tools._get_config_dirs", return_value=[temp_dir])
        
        patcher1.start()
        patcher2.start()
        patcher3.start()
        
        yield temp_dir
        
        patcher1.stop()
        patcher2.stop()
        patcher3.stop()