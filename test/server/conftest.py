import os
import shutil
import tempfile
import pytest
from unittest.mock import patch
from event_handlers import register_core_handlers
import itertools
from contextlib import contextmanager
from system_logger import logger
from registry import register_entity


@pytest.fixture(autouse=True)
def auto_register_entities():
    """
    Auto-registers all BaseGameEntity subclasses (Creature, MeleeWeapon, RangedWeapon, etc.)
    created during tests, since auto-registration was removed from model_post_init.
    This preserves test ergonomics without re-introducing implicit global state.

    Patching only BaseGameEntity.__init__ (the root of the inheritance chain) ensures
    registration fires exactly once per object regardless of how many subclass __init__s
    call super().__init__().
    """
    from dnd_rules_engine import BaseGameEntity

    original = BaseGameEntity.__init__

    def _auto_reg_init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        register_entity(self, getattr(self, "vault_path", "default"))

    BaseGameEntity.__init__ = _auto_reg_init
    yield
    BaseGameEntity.__init__ = original


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
        journals_dir = os.path.join(temp_dir, "server", "Journals")
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


def pytest_runtest_logreport(report):
    """Pytest hook to push test failures directly into the JSONL event log."""
    if report.when == 'call' and report.failed:
        logger.error(f"Pytest failure in {report.nodeid}", extra={
            "agent_id": "PYTEST",
            "context": {
                "client_id": "PYTEST_RUNNER",
                "character": "QA_SYSTEM",
                "vault_path": "TEST_VAULT",
                "duration_s": round(report.duration, 2),
                "longrepr": str(report.longrepr)
            }
        })
