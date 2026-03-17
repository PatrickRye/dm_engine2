import os
import shutil
import tempfile
import pytest
from unittest.mock import patch
from event_handlers import register_core_handlers
import itertools
from contextlib import contextmanager


@pytest.fixture
def mock_dice():
    """
    Fixture that provides a context manager to safely mock random.randint.
    It takes an arbitrary number of specific rolls and then infinitely yields a default value (10),
    preventing StopIteration errors when the engine makes unexpected additional checks.
    """

    @contextmanager
    def _mock_dice(*rolls, default=10):
        infinite_rolls = itertools.chain(rolls, itertools.repeat(default))
        with patch("random.randint", side_effect=infinite_rolls) as mocked_randint:
            yield mocked_randint

    return _mock_dice


@pytest.fixture
def mock_roll_dice():
    """
    Fixture that provides a context manager to safely mock roll_dice.
    It takes an arbitrary number of specific damage/healing rolls and then infinitely yields a default value,
    preventing StopIteration errors when the engine evaluates complex AoE damage.
    """

    @contextmanager
    def _mock_roll_dice(*rolls, default=10):
        infinite_rolls = itertools.chain(rolls, itertools.repeat(default))

        def side_effect(*args, **kwargs):
            return next(infinite_rolls)

        with patch("event_handlers.roll_dice", side_effect=side_effect) as m1, patch(
            "dnd_rules_engine.roll_dice", side_effect=side_effect
        ) as m2:
            yield m1

    return _mock_roll_dice


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
